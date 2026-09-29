"""Read the mails, ask the local model about each one, write the report."""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TextIO

from reakto import prompts
from reakto.llm import LLMError, LocalLLM
from reakto.mail import Mail, find_mail_files, read_mail
from reakto.mailbox import Mailbox, detect_my_addresses
from reakto.report import Item, check_target, fold_threads, render, write_report
from reakto.store import Cache, cache_key
from reakto.verdict import Verdict, parse_verdict, settle, worth_deep_review

REPAIR = (
    "Your previous reply was not a valid JSON object with the required keys. Reply again with "
    "ONLY the JSON object (summary, sender_type, kind, needs_action, action_type, action, "
    "priority, deadline, event_date, suspicious, superseded, confidence, reason)."
)


@dataclass
class Options:
    source: Path
    target: Path
    model: str
    url: str
    language: str = "fi"
    rules: str = ""
    me: list[str] = field(default_factory=list)
    deep: bool = True
    deep_max_tokens: int = 8192
    min_confidence: float = 0.75
    since_days: int | None = None
    limit: int | None = None
    match: list[str] = field(default_factory=list)
    refresh: bool = False
    today: date = field(default_factory=date.today)
    temperature: float = 0.3


class Progress:
    def __init__(self, out: TextIO = sys.stderr):
        self.out = out

    def log(self, msg: str) -> None:
        print(msg, file=self.out, flush=True)


def _short(v: Verdict) -> str:
    if v.error:
        return f"VIRHE: {v.error[:80]}"
    if not v.needs_action:
        return f"ei toimenpiteitä ({v.kind})"
    tag = "IHMINEN " if v.sender_type == "human" else "EPÄILYTTÄVÄ " if v.suspicious else ""
    extra = f" {v.deadline or v.event_date}" if (v.deadline or v.event_date) else ""
    return f"{tag}{v.priority.upper()} {v.action_type}{extra}: {v.action[:70]}"


class Engine:
    def __init__(self, opts: Options, llm: LocalLLM | None = None, cache: Cache | None = None,
                 progress: Progress | None = None):
        self.o = opts
        self.llm = llm or LocalLLM(base_url=opts.url, model=opts.model, temperature=opts.temperature)
        self.cache = cache or Cache()
        self.p = progress or Progress()
        self.system = prompts.system_prompt(opts.language, opts.rules)
        self.verdicts: dict[str, Verdict] = {}
        self.box: Mailbox | None = None
        self.targets: list[Mail] = []
        self._last_write = 0.0
        self._warm_pending = False

    # -- loading --------------------------------------------------------
    def load(self) -> None:
        files = find_mail_files(self.o.source)
        self.p.log(f"reakto: {len(files)} viestiä hakemistossa {self.o.source}")
        with ThreadPoolExecutor(max_workers=4) as ex:
            mails = [m for m in ex.map(read_mail, files) if m is not None]
        if len(mails) < len(files):
            self.p.log(f"  {len(files) - len(mails)} tiedostoa ei voitu lukea")
        me = detect_my_addresses(mails, self.o.me)
        self.box = Mailbox.build(mails, me)
        self.p.log(f"  omat osoitteet: {', '.join(sorted(me)) or '(ei tunnistettu)'}")
        seen: set[str] = set()
        targets = []
        for m in sorted(mails, key=lambda m: -m.sort_key):  # newest first
            if self.box.is_mine(m) or m.sha256 in seen:
                continue
            seen.add(m.sha256)
            targets.append(m)
        if self.o.since_days is not None:
            cutoff = datetime.now().astimezone() - timedelta(days=self.o.since_days)
            targets = [m for m in targets if m.date and m.date >= cutoff]
        if self.o.match:
            targets = [m for m in targets if any(s.lower() in m.path.name.lower() for s in self.o.match)]
        if self.o.limit:
            targets = targets[: self.o.limit]
        self.targets = targets
        mine = sum(1 for m in mails if self.box.is_mine(m))
        self.p.log(f"  analysoitavana {len(targets)} viestiä ({mine} omaa lähetettyä viestiä käytetään vain kontekstina)")

    # -- model calls ----------------------------------------------------
    def _ask(self, user: str, *, think: bool) -> tuple[Verdict, float]:
        messages = [{"role": "system", "content": self.system}, {"role": "user", "content": user}]
        t0 = time.monotonic()
        c = self.llm.complete_chat(self.system, user, think=think,
                                   max_tokens=self.o.deep_max_tokens if think else 1200)
        try:
            v = parse_verdict(c.content)
        except (ValueError, json.JSONDecodeError):
            messages += [{"role": "assistant", "content": c.content}, {"role": "user", "content": REPAIR}]
            c = self.llm.complete(messages, think=False, max_tokens=1200)
            v = parse_verdict(c.content)
        v.tokens = c.completion_tokens
        return v, time.monotonic() - t0

    def _key(self, stage: str, m: Mail, context: str) -> str:
        return cache_key(stage, m.sha256, self.llm.model, prompts.PROMPT_VERSION, self.o.language,
                         self.o.rules, context)

    def _context(self, m: Mail, summaries: dict[str, str] | None = None) -> str:
        assert self.box is not None
        replies = ",".join(r.sha256 for r in self.box.my_replies_after(m))
        return prompts.related_block(m, self.box, summaries) + "|" + replies + "|" + ",".join(sorted(self.box.me))

    def analyze(self, m: Mail, *, deep: bool, summaries: dict[str, str] | None = None,
                first: Verdict | None = None) -> tuple[Verdict, bool]:
        """(verdict, from_cache) for one mail and one pass."""
        assert self.box is not None
        stage = "deep" if deep else "quick"
        ctx = self._context(m, summaries if deep else None)
        if deep and first is not None:
            ctx += "|" + json.dumps(first.model_view(), sort_keys=True, ensure_ascii=False)
        key = self._key(stage, m, ctx)
        if not self.o.refresh:
            hit = self.cache.get(key)
            if hit:
                return Verdict.from_dict(hit), True
        if self._warm_pending:
            self._warm_pending = False
            self.warm_up()
        user = prompts.user_message(
            m, self.box, self.o.today, self.o.language,
            summaries=summaries if deep else None,
            first_pass=first.model_view() if deep and first is not None else None,
        )
        try:
            v, secs = self._ask(user, think=deep)
        except (LLMError, ValueError, json.JSONDecodeError) as e:
            v = Verdict(error=str(e)[:300], model=self.llm.model)
            return v, False
        v.deep, v.model, v.seconds = deep, self.llm.model, round(secs, 1)
        self.cache.put(key, m.sha256, stage, v.to_dict())
        return v, False

    def _settled(self, m: Mail, v: Verdict) -> Verdict:
        assert self.box is not None
        return settle(Verdict.from_dict(v.to_dict()), replied_after=bool(self.box.my_replies_after(m)),
                      today=self.o.today)

    def warm_up(self) -> None:
        """Checkpoint the shared prompt prefix (Ollama + Qwen raw path only)."""
        assert self.box is not None
        if not getattr(self.llm, "uses_raw_chatml", lambda: False)():
            return
        try:
            secs = self.llm.warm_prefix(self.system, prompts.user_prefix(self.box, self.o.today))
            self.p.log(f"  malli valmiina, yhteinen kehotteen alku välimuistissa ({secs:.0f}s)")
        except (LLMError, OSError) as e:
            self.p.log(f"  lämmitys epäonnistui, jatketaan ilman: {e}")

    # -- report -----------------------------------------------------------
    def write(self, *, done: bool, force: bool = False) -> None:
        if not force and time.monotonic() - self._last_write < 20:
            return
        assert self.box is not None
        items = [Item(m, self._settled(m, self.verdicts[m.sha256])) for m in self.targets if m.sha256 in self.verdicts]
        # Threads where a person wrote after my last reply (per the model's raw verdicts).
        waiting = set()
        for it in items:
            raw = self.verdicts[it.mail.sha256]
            if raw.sender_type == "human" and not raw.error and not self.box.my_replies_after(it.mail):
                waiting.add(self.box.thread_of.get(it.mail.sha256, it.mail.sha256))
        items = fold_threads(items, self.box.thread_of, waiting)
        text = render(items, today=self.o.today, source=self.o.source, model=self.llm.model,
                      total=len(self.targets), analyzed=len(items), done=done)
        write_report(self.o.target, text)
        self._last_write = time.monotonic()

    def _eta(self, times: list[float], left: int) -> str:
        if not times or left <= 0:
            return ""
        secs = sum(times[-20:]) / len(times[-20:]) * left
        return f", arviolta {secs / 60:.0f} min jäljellä" if secs >= 60 else ""

    # -- the run -----------------------------------------------------------
    def run(self) -> int:
        check_target(self.o.target)
        if self.box is None:
            self.load()
        n = len(self.targets)
        t_start = time.monotonic()
        times: list[float] = []
        dtimes: list[float] = []
        try:
            self.p.log(f"Vaihe 1/2: nopea analyysi kaikille ({self.llm.model})")
            self._warm_pending = True
            for i, m in enumerate(self.targets, 1):
                v, cached = self.analyze(m, deep=False)
                self.verdicts[m.sha256] = v
                s = self._settled(m, v)
                if not cached and not v.error:
                    times.append(v.seconds)
                timing = ("välimuisti" if cached else f"{v.seconds:.0f}s") + self._eta(times, n - i)
                self.p.log(f"[{i}/{n}] {m.date_str()[:10]} {(m.from_name or m.from_addr)[:28]} — {m.subject[:50]}\n"
                           f"        → {_short(s)} ({timing})")
                self.write(done=False)
            if self.o.deep:
                quick = dict(self.verdicts)
                flagged = [m for m in self.targets if worth_deep_review(quick[m.sha256], self.o.min_confidence, self.o.today)]
                summaries = {sha: v.summary for sha, v in quick.items() if v.summary}
                self.p.log(f"Vaihe 2/2: syväanalyysi (thinking) {len(flagged)} viestille, jotka nousivat esiin")
                self._warm_pending = True
                self.write(done=False, force=True)
                for i, m in enumerate(flagged, 1):
                    first = quick[m.sha256]
                    v, cached = self.analyze(m, deep=True, summaries=summaries, first=None if first.error else first)
                    if v.error:
                        self.p.log(f"  syväanalyysi epäonnistui, käytetään nopeaa tulosta: {v.error[:100]}")
                        if first.error:
                            self.verdicts[m.sha256] = v
                    else:
                        self.verdicts[m.sha256] = v
                    s = self._settled(m, self.verdicts[m.sha256])
                    if not cached and not v.error:
                        dtimes.append(v.seconds)
                    timing = ("välimuisti" if cached else f"{v.seconds:.0f}s") + self._eta(dtimes, len(flagged) - i)
                    self.p.log(f"[syvä {i}/{len(flagged)}] {m.date_str()[:10]} {(m.from_name or m.from_addr)[:28]} — {m.subject[:50]}\n"
                               f"        → {_short(s)} ({timing})")
                    self.write(done=False)
        except KeyboardInterrupt:
            self.write(done=False, force=True)
            self.p.log(f"Keskeytetty. Osittainen raportti: {self.o.target}")
            return 130
        self.write(done=True, force=True)
        need = sum(1 for m in self.targets if self._settled(m, self.verdicts[m.sha256]).needs_action)
        self.p.log(f"Valmis {time.monotonic() - t_start:.0f} s: {need}/{n} viestiä vaatii reaktiota → {self.o.target}")
        return 0
