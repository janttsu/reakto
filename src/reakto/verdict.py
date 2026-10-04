"""The model's verdict on one message, validated and post-processed."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, fields
from datetime import date

from reakto.llm import extract_json_object

KINDS = {
    "personal", "work", "invoice", "order", "delivery", "booking", "ticket_or_trip",
    "authority_or_bank", "support_ticket", "account_security", "verification_code", "receipt",
    "newsletter", "marketing", "notification", "other",
}
ACTIONS = {
    "reply", "pay", "confirm", "read_elsewhere", "attend", "await_delivery", "pick_up",
    "check_security", "decide", "other", "none",
}
PRIORITIES = ["critical", "high", "medium", "low", "none"]


@dataclass
class Verdict:
    summary: str = ""
    sender_type: str = "automated"
    kind: str = "other"
    needs_action: bool = False
    action_type: str = "none"
    action: str = ""
    priority: str = "none"
    deadline: str = ""
    event_date: str = ""
    suspicious: bool = False
    superseded: bool = False
    confidence: float = 0.0
    reason: str = ""
    deep: bool = False
    model: str = ""
    seconds: float = 0.0
    tokens: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Verdict:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def model_view(self) -> dict:
        """What the deep review is shown of the first pass."""
        d = self.to_dict()
        for k in ("deep", "model", "seconds", "tokens", "error"):
            d.pop(k)
        return d

    @property
    def rank(self) -> int:
        return PRIORITIES.index(self.priority) if self.priority in PRIORITIES else len(PRIORITIES)


def _bool(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"true", "yes", "1", "kyllä"}
    return bool(v)


def _iso_date(v) -> str:
    s = str(v or "").strip()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
        if not m:
            return ""
        d, mo, y = map(int, m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return ""


def _choice(v, allowed, default: str) -> str:
    s = str(v or "").strip().lower().replace("-", "_").replace(" ", "_")
    return s if s in allowed else default


def parse_verdict(text: str) -> Verdict:
    d = extract_json_object(text)
    if not any(k in d for k in ("needs_action", "summary", "action_type")):
        raise ValueError("JSON has none of the verdict keys")
    try:
        conf = max(0.0, min(1.0, float(d.get("confidence", 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    return Verdict(
        summary=str(d.get("summary") or "")[:1500],
        sender_type="human" if str(d.get("sender_type", "")).strip().lower() == "human" else "automated",
        kind=_choice(d.get("kind"), KINDS, "other"),
        needs_action=_bool(d.get("needs_action", False)),
        action_type=_choice(d.get("action_type"), ACTIONS, "other"),
        action=str(d.get("action") or "")[:600],
        priority=_choice(d.get("priority"), PRIORITIES, "medium"),
        deadline=_iso_date(d.get("deadline")),
        event_date=_iso_date(d.get("event_date")),
        suspicious=_bool(d.get("suspicious", False)),
        superseded=_bool(d.get("superseded", False)),
        confidence=conf,
        reason=str(d.get("reason") or "")[:800],
    )


def settle(v: Verdict, *, replied_after: bool, today: date) -> Verdict:
    """Deterministic policy on top of the model's answer.

    - A real person writing to me is always "critical", unless I already replied.
    - Suspicious mail is always listed (as a warning).
    - Superseded mail and past bookings drop out; overdue invoices do not.
    """
    if v.error:
        return v
    if v.suspicious:
        v.needs_action = True
        v.action_type = "check_security"
        if v.rank > PRIORITIES.index("high"):
            v.priority = "high"
        return v
    if v.superseded:
        v.needs_action = False
    if v.sender_type == "human" and not v.superseded:
        if replied_after:
            v.needs_action = False
        else:
            v.needs_action = True
            v.priority = "critical"
            if v.action_type in ("none", "other"):
                v.action_type = "reply"
    if v.event_date and v.event_date < today.isoformat() and v.action_type in ("attend", "none", "other"):
        v.needs_action = False
    elif (v.kind in ("booking", "ticket_or_trip") and v.event_date >= today.isoformat()
          and not v.superseded and v.action_type != "attend"):
        # A confirmed booking in the future always goes on the list, even if the model shrugs.
        v.needs_action = True
        v.action_type = "attend"
        if not v.action:
            v.action = "Muista varaus (tai peru ajoissa)."
        days = (date.fromisoformat(v.event_date) - today).days
        v.priority = "high" if days <= 1 else "medium" if days <= 7 else "low"
    if v.kind == "order" and not v.superseded and not v.needs_action:
        # Every order (goods, a subscription, a service change) stays on the list at a low
        # level until a later mail shows it was delivered: check its contents and that it happens.
        v.needs_action = True
        v.action_type = "await_delivery"
        v.priority = "low"
        v.action = v.action or "Tarkista tilauksen sisältö ja varmista, että tilaus toteutuu."
    if v.needs_action and v.priority == "none":
        v.priority = "low"
    if not v.needs_action:
        v.priority = "none"
        v.action_type = "none"
    return v


# Kinds where a quick "nothing to do" is often wrong, so the deep pass looks at them anyway.
GRAY_KINDS = {
    "personal", "work", "invoice", "order", "delivery", "booking", "ticket_or_trip",
    "authority_or_bank", "support_ticket",
}


def worth_deep_review(v: Verdict, min_confidence: float, today: date, *, gray: bool = True) -> bool:
    """Pass 2 candidates: what will be reported, plus (after a quick pass) the gray zone."""
    if v.error or v.needs_action or v.sender_type == "human" or v.suspicious or v.confidence < min_confidence:
        return True
    if not gray or v.kind not in GRAY_KINDS:
        return False
    # A trip or booking that is over, with nothing due, drops out whatever the review says.
    return not (v.event_date and v.event_date < today.isoformat() and not v.deadline)
