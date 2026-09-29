"""The Markdown report of the mails that need my reaction."""

from __future__ import annotations

import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote

from reakto.mail import Mail
from reakto.verdict import Verdict

MARKER = "<!-- reakto report: generated file, rewritten on every run -->"

PRIORITY_FI = {"critical": "🔴 kriittinen", "high": "🟠 korkea", "medium": "🟡 keski", "low": "⚪ matala"}
ACTION_FI = {
    "reply": "vastaa", "pay": "maksa", "confirm": "vahvista", "read_elsewhere": "lue palvelussa",
    "attend": "varaus", "await_delivery": "odota toimitusta", "pick_up": "nouda",
    "check_security": "tarkista", "decide": "päätä", "other": "muu", "none": "",
}


class TargetNotOurs(RuntimeError):
    pass


def check_target(target: Path) -> None:
    """Only a file reakto wrote itself may be replaced."""
    if target.exists():
        if not target.is_file():
            raise TargetNotOurs(f"{target} exists and is not a regular file")
        with open(target, encoding="utf-8", errors="replace") as f:
            first = f.readline().strip()
        if first != MARKER:
            raise TargetNotOurs(
                f"{target} exists and was not written by reakto; refusing to overwrite it. "
                "Give another target file."
            )
    if not target.parent.is_dir():
        raise TargetNotOurs(f"directory {target.parent} does not exist")


@dataclass
class Item:
    mail: Mail
    verdict: Verdict
    also: list[Mail] = field(default_factory=list)  # older mails of the same thread, folded in


def fold_threads(items: list[Item], thread_of: dict[str, str], human_waiting: set[str]) -> list[Item]:
    """One entry per conversation: its newest mail that needs action.

    Older actionable mails of the same thread are listed under it instead of
    repeating the matter. A thread with a person still waiting for my answer
    (``human_waiting``: thread ids) is escalated to critical.
    """
    groups: dict[str, list[Item]] = {}
    for it in items:
        groups.setdefault(thread_of.get(it.mail.sha256, it.mail.sha256), []).append(it)
    out: list[Item] = []
    for tid, group in groups.items():
        act = [i for i in group if i.verdict.needs_action and not i.verdict.error]
        rest = [i for i in group if i not in act]
        out.extend(rest)
        if not act:
            continue
        act.sort(key=lambda i: -i.mail.sort_key)
        # A suspicious mail is never folded into a genuine thread (or vice versa).
        for sus in (True, False):
            sub = [i for i in act if i.verdict.suspicious is sus]
            if not sub:
                continue
            head = sub[0]
            head.also = [i.mail for i in sub[1:]]
            if not sus and tid in human_waiting and head.verdict.priority != "critical":
                head.verdict.priority = "critical"
                head.verdict.sender_type = "human"
                if head.verdict.action_type in ("none", "other"):
                    head.verdict.action_type = "reply"
            out.append(head)
            for i in sub[1:]:
                i.verdict.needs_action = False  # shown under the head entry
                i.verdict.priority = "none"
                out.append(i)
    return out


def _days(iso: str, today: date) -> int | None:
    try:
        return (date.fromisoformat(iso) - today).days
    except ValueError:
        return None


def _when(iso: str, today: date, past_word: str) -> str:
    d = _days(iso, today)
    if d is None:
        return iso
    if d < 0:
        return f"{iso} — **{past_word} {-d} pv sitten**"
    if d == 0:
        return f"{iso} — **tänään**"
    if d == 1:
        return f"{iso} — **huomenna**"
    return f"{iso} — {d} pv päästä"


def _link(p: Path) -> str:
    return f"[{p.name}](file://{quote(str(p.resolve()))})"


def _sender(m: Mail) -> str:
    return f"{m.from_name} <{m.from_addr}>" if m.from_name else m.from_addr


def _cell(s: str, n: int = 70) -> str:
    s = " ".join(s.split()).replace("|", "/")
    return s if len(s) <= n else s[: n - 1] + "…"


def _entry(it: Item, today: date) -> list[str]:
    m, v = it.mail, it.verdict
    out = [f"### {m.subject or '(ei aihetta)'}", f"{_sender(m)} · {m.date_str()}", ""]
    if v.action:
        out.append(f"- **Toimenpide ({ACTION_FI.get(v.action_type, v.action_type)}):** {v.action}")
    if v.deadline:
        out.append(f"- **Määräaika:** {_when(v.deadline, today, 'ERÄÄNTYNYT')}")
    if v.event_date:
        out.append(f"- **Ajankohta:** {_when(v.event_date, today, 'mennyt')}")
    out.append(f"- **Tärkeys:** {PRIORITY_FI.get(v.priority, v.priority)}"
               f" · {'ihminen' if v.sender_type == 'human' else 'automaattinen'}"
               f" · varmuus {v.confidence:.0%}{' · syväanalyysi' if v.deep else ''}")
    if v.summary:
        out.append(f"- **Analyysi:** {v.summary}")
    if v.reason:
        out.append(f"- **Perustelu:** {v.reason}")
    out.append(f"- **Viesti:** {_link(m.path)}")
    if it.also:
        out.append(f"- **Samassa ketjussa myös ({len(it.also)}):** "
                   + ", ".join(f"{o.date_str()[:10]} {_link(o.path)}" for o in it.also))
    out.append("")
    return out


def _sort_key(it: Item) -> tuple:
    v = it.verdict
    return (v.rank, v.deadline or v.event_date or "9999", -it.mail.sort_key)


def render(
    items: list[Item], *, today: date, source: Path, model: str, total: int, analyzed: int,
    done: bool, generated: datetime | None = None,
) -> str:
    act = [it for it in items if it.verdict.needs_action and not it.verdict.error]
    errors = [it for it in items if it.verdict.error]
    human = sorted([i for i in act if i.verdict.sender_type == "human" and not i.verdict.suspicious], key=_sort_key)
    suspicious = sorted([i for i in act if i.verdict.suspicious], key=_sort_key)
    rest = [i for i in act if i not in human and i not in suspicious]
    bookings = sorted([i for i in rest if i.verdict.action_type == "attend"],
                      key=lambda i: (i.verdict.event_date or "9999", -i.mail.sort_key))
    deliveries = sorted([i for i in rest if i.verdict.action_type == "await_delivery"], key=lambda i: -i.mail.sort_key)
    todo = [i for i in rest if i not in bookings and i not in deliveries]
    minor = sorted([i for i in todo if i.verdict.priority == "low"], key=_sort_key)
    todo = sorted([i for i in todo if i.verdict.priority != "low"], key=_sort_key)

    gen = (generated or datetime.now()).strftime("%Y-%m-%d %H:%M")
    state = "valmis" if done else f"KESKEN – {analyzed}/{total} viestiä analysoitu"
    lines = [
        MARKER,
        "# Sähköpostit, jotka vaativat reaktiotasi",
        "",
        f"Lähde `{source}` · {total} viestiä · malli `{model}` (paikallinen) · {gen} · **{state}**",
        "",
        f"**{len(act)} vaatii reaktiota:** {len(human)} ihmisiltä · {len(todo)} toimenpidettä · "
        f"{len(bookings)} varausta · {len(deliveries)} odottaa toimitusta · {len(suspicious)} epäilyttävää · "
        f"{len(minor)} pientä tarkistusta" + (f" · {len(errors)} analyysi epäonnistui" if errors else ""),
        "",
    ]
    overview = human + suspicious + todo + bookings + deliveries + minor
    if overview:
        lines += ["| Tärkeys | Pvm | Lähettäjä | Aihe | Toimenpide | Määräaika |", "|---|---|---|---|---|---|"]
        for it in overview:
            m, v = it.mail, it.verdict
            dl = v.deadline or v.event_date
            d = _days(dl, today) if dl else None
            dl_txt = (f"**{dl} (myöhässä)**" if d is not None and d < 0 and v.deadline else dl) if dl else ""
            lines.append(
                f"| {PRIORITY_FI.get(v.priority, v.priority)} | {m.date_str()[:10]} | {_cell(m.from_name or m.from_addr, 30)} "
                f"| {_cell(m.subject, 50)} | {_cell(v.action or ACTION_FI.get(v.action_type, ''), 60)} | {dl_txt} |"
            )
        lines.append("")

    def section(title: str, note: str, group: list[Item]) -> None:
        if not group:
            return
        lines.extend([f"## {title} ({len(group)})", ""] + ([note, ""] if note else []))
        for it in group:
            lines.extend(_entry(it, today))

    section("🔴 Erittäin tärkeät – oikeiden ihmisten viestit", "Ihmisen itse kirjoittama viesti, johon et ole vielä vastannut.", human)
    section("⚠️ Epäilyttävät viestit", "Mahdollinen huijaus: älä avaa linkkejä, tarkista asia palvelusta itse.", suspicious)
    section("🟠 Toimenpiteet", "Maksut, vastaukset, vahvistukset ja muualla odottavat viestit.", todo)
    section("📅 Tulevat varaukset ja tapahtumat", "", bookings)
    section("📦 Odottaa toimitusta", "Tilaukset, joiden toimituksesta ei ole vielä vahvistusta.", deliveries)
    section("🔎 Pienet tarkistukset", "Matalan prioriteetin asiat, esimerkiksi kirjautumisilmoitukset.", minor)
    if errors:
        lines += [f"## ❓ Analyysi epäonnistui – tarkista itse ({len(errors)})", ""]
        for it in errors:
            lines.append(f"- {it.mail.date_str()} {_sender(it.mail)}: {it.mail.subject} — {_link(it.mail.path)} ({it.verdict.error[:120]})")
        lines.append("")

    no_action = [it for it in items if not it.verdict.needs_action and not it.verdict.error]
    kinds = Counter(it.verdict.kind for it in no_action)
    lines += [
        "## Ei vaadi reaktiota",
        "",
        f"{len(no_action)} viestiä: " + ", ".join(f"{k} {n}" for k, n in kinds.most_common()) + ".",
        "",
    ]
    return "\n".join(lines)


def write_report(target: Path, text: str) -> None:
    """Atomic replace, private permissions; check_target() must have passed."""
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, 0o600)
        check_target(target)  # re-check right before replacing
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
