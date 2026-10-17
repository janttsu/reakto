"""Live mail from a Tuta mailbox through a local tutamcp server.

reakto starts the MCP server (``tutamcp.sh``) as a child process, lists the
inbox, reads each mail's body and PDF attachments, and can afterwards move
the mails into folders by their verdict. Mail text goes only to the local
language model; the MCP server itself talks to Tuta over HTTPS and decrypts
on this machine.
"""

from __future__ import annotations

import email.header
import email.utils
import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from reakto.mail import (
    _NOREPLY_RE,
    ATTACHMENT_TEXT_CHARS,
    BODY_CHARS,
    MAX_ATTACHMENT_TEXTS,
    Attachment,
    Mail,
    _pdf_text,
    clean_text,
)
from reakto.mcp import MCPClient, MCPError
from reakto.verdict import PRIORITIES, Verdict

PAGE = 100
NEWSLETTER_KINDS = {"newsletter", "marketing"}


@dataclass
class Folder:
    list_id: str      # "id" in tutamcp: pass to tuta_mail_list
    elem_id: str      # folder_elem_id: target of tuta_mail_move
    folder_list_id: str
    name: str
    type: str


@dataclass
class TutaSource:
    command: str
    folder: str = "inbox"            # name, type or id of the folder to triage
    important: str = ""              # folder (name or elem id) for mails that need my reaction
    newsletters: str = ""            # folder for newsletters and marketing
    archive: str = "archive"         # folder for everything else
    important_min_priority: str = "low"  # verdicts below this priority go to the archive instead
    with_sent: bool = True           # read the Sent folder to know what I already answered
    sent_limit: int = 200
    limit: int | None = None
    since_days: int | None = None
    pdf_text: bool = True
    timeout: float = 600.0
    folders: list[Folder] = field(default_factory=list)
    log = None

    def label(self) -> str:
        return f"Tuta {self.folder} (live)"

    # -- helpers -------------------------------------------------------------------
    def _say(self, msg: str) -> None:
        if self.log:
            self.log(msg)

    def _client(self) -> MCPClient:
        return MCPClient(self.command, timeout=self.timeout)

    def _load_folders(self, c: MCPClient) -> None:
        self.folders = [
            Folder(list_id=str(f.get("id", "")), elem_id=str(f.get("folder_elem_id", "")),
                   folder_list_id=str(f.get("folder_list_id", "")), name=str(f.get("name", "")),
                   type=str(f.get("type", "")))
            for f in _as_list(c.call("tuta_mail_list_folders")) if isinstance(f, dict)
        ]

    def find_folder(self, key: str) -> Folder | None:
        k = key.strip().lower()
        for f in self.folders:
            if k in (f.elem_id.lower(), f.list_id.lower()):
                return f
        for f in self.folders:
            if k in (f.name.lower(), f.type.lower()):
                return f
        return None

    # -- fetching ----------------------------------------------------------------------
    def fetch(self) -> list[Mail]:
        with self._client() as c:
            self._load_folders(c)
            inbox = self.find_folder(self.folder)
            if inbox is None:
                names = ", ".join(f"{f.name} ({f.type})" for f in self.folders)
                raise MCPError(f"folder {self.folder!r} not found; folders: {names}")
            cutoff = None
            if self.since_days is not None:
                cutoff = datetime.now(UTC).timestamp() - self.since_days * 86400
            summaries = self._list(c, inbox, self.limit, cutoff)
            self._say(f"  Tuta {inbox.name}: {len(summaries)} viestiä haettavana")
            mails: list[Mail] = []
            for i, s in enumerate(summaries, 1):
                m = self._read(c, s)
                if m is not None:
                    mails.append(m)
                if i % 10 == 0:
                    self._say(f"  luettu {i}/{len(summaries)}")
            if self.with_sent:
                sent = self.find_folder("sent")
                if sent is not None:
                    for s in self._list(c, sent, self.sent_limit, cutoff):
                        m = _mail_from_summary(s, mine=True)
                        if m is not None:
                            mails.append(m)
            return mails

    def _list(self, c: MCPClient, folder: Folder, limit: int | None, cutoff: float | None) -> list[dict]:
        out: list[dict] = []
        before = None
        while True:
            want = PAGE if limit is None else min(PAGE, limit - len(out))
            if want <= 0:
                break
            page = _as_list(c.call("tuta_mail_list", folder_id=folder.list_id, limit=want, before_id=before))
            if not page:
                break
            stop = False
            for s in page:
                if cutoff is not None and _ts(s.get("date")) < cutoff:
                    stop = True
                    break
                out.append(s)
            if stop or len(page) < want:
                break
            before = page[-1].get("id")
            if not before:
                break
        return out

    def _read(self, c: MCPClient, s: dict) -> Mail | None:
        try:
            r = c.call("tuta_mail_read", list_id=s["list_id"], mail_id=s["id"])
        except (MCPError, KeyError) as e:
            self._say(f"  viestiä {s.get('id')} ei voitu lukea: {e}")
            return _mail_from_summary(s, mine=False)
        if not isinstance(r, dict) or r.get("error"):
            return _mail_from_summary(s, mine=False)
        m = _mail_from_summary({**s, **{k: r[k] for k in ("subject", "from", "date") if k in r}}, mine=False)
        if m is None:
            return None
        m.to = _addrs(r.get("to"))
        m.cc = _addrs(r.get("cc"))
        m.body = clean_text(str(r.get("body") or ""), BODY_CHARS)
        if re.search(r"unsubscribe|peruuta tilaus|lopeta tilaus|avsluta prenumeration", m.body, re.I):
            m.signals["unsubscribe_link_in_body"] = "yes"
        texts = 0
        for a in r.get("attachments") or []:
            if not isinstance(a, dict):
                continue
            att = Attachment(filename=str(a.get("name") or ""), content_type=str(a.get("mime") or ""), size=0)
            is_pdf = att.content_type == "application/pdf" or att.filename.lower().endswith(".pdf")
            if self.pdf_text and is_pdf and texts < MAX_ATTACHMENT_TEXTS and "index" in a:
                text = self._attachment_text(c, s, int(a["index"]))
                if text:
                    att.text = clean_text(text, ATTACHMENT_TEXT_CHARS)
                    texts += 1
            m.attachments.append(att)
        return m

    def _attachment_text(self, c: MCPClient, s: dict, index: int) -> str:
        """Let the server download the PDF, read its text, and remove our own temporary copy."""
        try:
            r = c.call("tuta_mail_get_attachment", list_id=s["list_id"], mail_id=s["id"], attachment_index=index)
        except MCPError as e:
            self._say(f"  liitettä ei saatu: {e}")
            return ""
        path = r.get("local_path") if isinstance(r, dict) else None
        if not path or not os.path.isfile(path):
            return ""
        try:
            with open(path, "rb") as f:
                data = f.read(10 * 1024 * 1024)
            text = _pdf_text(data)
        finally:
            try:
                os.unlink(path)  # a copy the server made for this call; the mail still holds the file
            except OSError:
                pass
        return text

    # -- organizing ------------------------------------------------------------------------
    def destination(self, v: Verdict) -> str:
        if v.error:
            return ""
        if v.needs_action:
            if v.rank <= PRIORITIES.index(self.important_min_priority if self.important_min_priority in PRIORITIES else "low"):
                return self.important
            return self.archive
        if v.kind in NEWSLETTER_KINDS:
            return self.newsletters
        return self.archive

    def organize(self, decided: list[tuple[Mail, Verdict]], *, dry_run: bool = False) -> dict[str, int]:
        """Move each triaged inbox mail to the folder its verdict points at."""
        moved: dict[str, int] = {}
        with self._client() as c:
            self._load_folders(c)
            targets: dict[str, Folder] = {}
            for key in (self.important, self.newsletters, self.archive):
                if key:
                    f = self.find_folder(key)
                    if f is None:
                        raise MCPError(f"target folder {key!r} not found")
                    targets[key] = f
            for m, v in decided:
                key = self.destination(v)
                if not key or not m.locator:
                    continue
                f = targets[key]
                list_id, mail_id = m.locator.split("/", 1)
                if not dry_run:
                    c.call("tuta_mail_move", list_id=list_id, mail_id=mail_id,
                           target_folder_list_id=f.folder_list_id, target_folder_elem_id=f.elem_id)
                moved[f.name] = moved.get(f.name, 0) + 1
        return moved


# -- conversions ---------------------------------------------------------------------------------
def _as_list(result) -> list:
    """A one-element list comes back from the MCP text content as a bare dict."""
    if isinstance(result, dict):
        if result.get("error"):
            raise MCPError(str(result["error"]))
        return [result]
    return [x for x in (result or []) if isinstance(x, dict)]


def _decode(s: str) -> str:
    try:
        return str(email.header.make_header(email.header.decode_header(s or "")))
    except (UnicodeError, ValueError, LookupError):
        return s or ""


def _addrs(v) -> list[str]:
    if isinstance(v, str):
        v = [v]
    out = []
    for item in v or []:
        _, a = email.utils.parseaddr(_decode(str(item)))
        if "@" in a:
            out.append(a.lower())
    return out


def _ts(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _mail_from_summary(s: dict, *, mine: bool) -> Mail | None:
    mail_id, list_id = str(s.get("id") or ""), str(s.get("list_id") or "")
    if not mail_id:
        return None
    name, addr = email.utils.parseaddr(_decode(str(s.get("from") or "")))
    subject = " ".join(_decode(str(s.get("subject") or "")).split())
    try:
        date = datetime.fromisoformat(str(s.get("date")))
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        date = None
    body = ""
    sha = hashlib.sha256(f"tuta:{list_id}/{mail_id}|{s.get('date')}|{subject}".encode()).hexdigest()
    signals: dict[str, str] = {}
    if addr and _NOREPLY_RE.search(addr.split("@")[0] + "@"):
        signals["noreply_sender"] = "yes"
    return Mail(
        path=Path(f"tuta/{_safe(subject) or mail_id}.mail"),
        sha256=sha,
        message_id=f"<{mail_id}@tuta>",
        in_reply_to=[],
        references=[],
        date=date,
        from_name=name.strip().strip('"'),
        from_addr=addr.lower(),
        reply_to="",
        to=_addrs(s.get("to")),
        cc=[],
        delivered_to=[],
        subject=subject,
        body=body,
        attachments=[],
        signals=signals,
        locator=f"{list_id}/{mail_id}",
        mine=mine,
    )


def _safe(s: str) -> str:
    return re.sub(r"[^\w .,()!?-]+", "_", s)[:80].strip()
