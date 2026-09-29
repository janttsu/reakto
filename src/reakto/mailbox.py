"""Mailbox-wide context: who "me" is, threads, and related messages.

The model judges one message at a time, but whether it still needs a reaction
often depends on the others: a later "your order was delivered", or my own
reply in the same thread. This module finds those links cheaply from headers.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from reakto.mail import Mail

_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*((re|fw|fwd|vs|vl|sv|aw|wg|tr)\s*(\[\d+\])?\s*:\s*|\[[^\]]{1,60}\]\s*"
    r"|(pending request|conversation ended)\s*:\s*)+",
    re.I,
)
_REF_NUMBER_RE = re.compile(r"(?<![\d.:/-])(?<!\d,)[A-Z]{0,3}#?(\d{5,})(?![\d.:/-]|,\d)")
RELATED_WINDOW_DAYS = 60
MAX_RELATED = 10


def base_domain(domain: str) -> str:
    parts = domain.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_subject(subject: str) -> str:
    return " ".join(_SUBJECT_PREFIX_RE.sub("", subject or "").lower().split())


def reference_numbers(subject: str) -> set[str]:
    """Order, ticket and invoice numbers (5+ digits) in a subject."""
    return {m.group(1) for m in _REF_NUMBER_RE.finditer(subject or "")}


def detect_my_addresses(mails: list[Mail], configured: list[str] | None = None) -> set[str]:
    """Addresses that received a large share of the mail, plus configured ones."""
    me = {a.lower() for a in configured or []}
    counts: Counter[str] = Counter()
    for m in mails:
        for a in set(m.to + m.delivered_to):
            counts[a] += 1
    for addr, c in counts.items():
        if c >= max(3, 0.1 * len(mails)):
            me.add(addr)
    return me


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class Mailbox:
    mails: list[Mail]
    me: set[str]
    thread_of: dict[str, str] = field(default_factory=dict)  # sha256 -> thread id
    threads: dict[str, list[Mail]] = field(default_factory=dict)
    by_domain: dict[str, list[Mail]] = field(default_factory=dict)

    @classmethod
    def build(cls, mails: list[Mail], me: set[str]) -> Mailbox:
        box = cls(mails=sorted(mails, key=lambda m: m.sort_key), me=me)
        uf = _UnionFind()
        for m in box.mails:
            node = "m:" + m.sha256
            uf.find(node)
            for mid in [m.message_id, *m.in_reply_to, *m.references]:
                if mid:
                    uf.union(node, "id:" + mid)
            subj = normalize_subject(m.subject)
            if len(subj) >= 8:
                # My replies are linked by subject too: some clients drop References.
                uf.union(node, "subj:" + subj)
            if m.from_addr not in me and m.from_domain:
                for num in reference_numbers(m.subject):
                    uf.union(node, f"num:{base_domain(m.from_domain)}:{num}")
        groups: dict[str, list[Mail]] = defaultdict(list)
        for m in box.mails:
            root = uf.find("m:" + m.sha256)
            box.thread_of[m.sha256] = root
            groups[root].append(m)
        box.threads = dict(groups)
        doms: dict[str, list[Mail]] = defaultdict(list)
        for m in box.mails:
            if m.from_domain and m.from_addr not in me:
                doms[base_domain(m.from_domain)].append(m)
        box.by_domain = dict(doms)
        return box

    def is_mine(self, m: Mail) -> bool:
        return m.from_addr in self.me

    def thread(self, m: Mail) -> list[Mail]:
        return self.threads.get(self.thread_of.get(m.sha256, ""), [m])

    def my_replies_after(self, m: Mail) -> list[Mail]:
        return [o for o in self.thread(m) if self.is_mine(o) and o.sort_key > m.sort_key]

    def addressed_to_me(self, m: Mail) -> str:
        if any(a in self.me for a in m.to):
            return "to"
        if any(a in self.me for a in m.cc):
            return "cc"
        return "other"  # bcc, mailing list or an alias

    def related(self, m: Mail) -> list[Mail]:
        """Same thread first, then the same sender domain near in time; later mails preferred."""
        seen = {m.sha256}
        out: list[Mail] = []
        for o in sorted(self.thread(m), key=lambda o: abs(o.sort_key - m.sort_key)):
            if o.sha256 not in seen and len(out) < MAX_RELATED:
                out.append(o)
                seen.add(o.sha256)
        window = RELATED_WINDOW_DAYS * 86400
        same = [
            o for o in self.by_domain.get(base_domain(m.from_domain), [])
            if o.sha256 not in seen and abs(o.sort_key - m.sort_key) <= window
        ]
        later = [o for o in same if o.sort_key >= m.sort_key]
        earlier = [o for o in same if o.sort_key < m.sort_key][::-1]
        room = MAX_RELATED - len(out)
        if room > 0:
            n_later = min(len(later), max(room - 3, (room + 1) // 2))
            out += (later[:n_later] + earlier)[:room]
            if len(out) < MAX_RELATED:
                out += later[n_later : n_later + MAX_RELATED - len(out)]
        return sorted(out, key=lambda o: o.sort_key)
