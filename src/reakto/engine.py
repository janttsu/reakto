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
    think: bool = True  # thinking on for every mail in pass 1
    deep: bool = True   # pass 2: a second, thinking review of what will be reported
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
        self._models: list[str] | None = None
        self.first: dict[str, Verdict] = {}  # pass 1 verdicts; self.verdicts holds the final ones
        self.known: dict[str, Verdict] = {}  # cached pass 1 verdicts of mails outside this run

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
        try:
            c = self.llm.complete_chat(self.system, user, think=think, max_tokens=self._budget(user, think))
        except LLMError as e:
            if not think or "budget" not in str(e):
                raise
            # Endless deliberation: answer this one mail without thinking rather than not at all.
            think = False
            c = self.llm.complete_chat(self.system, user, think=False, max_tokens=1200)
        try:
            v = parse_verdict(c.content)
        except (ValueError, json.JSONDecodeError):
            messages += [{"role": "assistant", "content": c.content}, {"role": "user", "content": REPAIR}]
            c = self.llm.complete(messages, think=False, max_tokens=1200)
            v = parse_verdict(c.content)
        v.tokens = c.completion_tokens
        v.deep = think
        return v, time.monotonic() - t0

    def _budget(self, user: str, think: bool) -> int:
        """Answer tokens: the thinking budget, cut to what the model's context leaves."""
        want = self.o.deep_max_tokens if think else 1200
        ctx = getattr(self.llm, "context_window", lambda: None)()
        if not ctx:
            return want
        prompt_est = (len(self.system) + len(user)) // 3 + 64  # Finnish runs ~3 chars/token
        return max(1024, min(want, ctx - prompt_est - 256))

    def _key(self, stage: str, m: Mail, context: str, model: str | None = None) -> str:
        return cache_key(stage, m.sha256, model or self.llm.model, prompts.PROMPT_VERSION, self.o.language,
                         self.o.rules, context)

    def _cached(self, stage: str, m: Mail, context: str) -> Verdict | None:
        """This model's verdict, else one from another model for the very same mail and context.

        Switching models must not hide the analyses already made; --refresh asks again.
        """
        if self.o.refresh:
            return None
        if self._models is None:
            self._models = [self.llm.model] + [x for x in self.cache.models() if x != self.llm.model]
        for model in self._models:
            hit = self.cache.get(self._key(stage, m, context, model))
            if hit:
                return Verdict.from_dict(hit)
        return None

    def _context(self, m: Mail, summaries: dict[str, str] | None = None) -> str:
        assert self.box is not None
        replies = ",".join(r.sha256 for r in self.box.my_replies_after(m))
        return prompts.related_block(m, self.box, summaries) + "|" + replies + "|" + ",".join(sorted(self.box.me))

    def analyze(self, m: Mail, *, deep: bool, summaries: dict[str, str] | None = None,
                first: Verdict | None = None, cache_only: bool = False) -> tuple[Verdict, bool]:
        """(verdict, from_cache) for one mail and one pass."""
        assert self.box is not None
        think = deep or self.o.think
        stage = ("review" if deep else "first") + ("-think" if think else "")
        ctx = self._context(m, summaries if deep else None)
        if deep and first is not None:
            ctx += "|" + json.dumps(first.model_view(), sort_keys=True, ensure_ascii=False)
        key = self._key(stage, m, ctx)
        hit = self._cached(stage, m, ctx)
        if hit is not None:
            return hit, True
        if cache_only:
            return Verdict(error="not cached"), False
        if self._warm_pending:
            self._warm_pending = False
            self.warm_up()
        user = prompts.user_message(
            m, self.box, self.o.today, self.o.language,
            summaries=summaries if deep else None,
            first_pass=first.model_view() if deep and first is not None else None,
        )
        try:
            v, secs = self._ask(user, think=think)
        except (LLMError, ValueError, json.JSONDecodeError) as e:
            v = Verdict(error=str(e)[:300], model=self.llm.model)
            return v, False
        v.model, v.seconds = self.llm.model, round(secs, 1)
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
    def _candidates(self) -> list[Mail]:
        return [m for m in self.targets if m.sha256 in self.first and worth_deep_review(
            self.first[m.sha256], self.o.min_confidence, self.o.today, gray=not self.o.think)]

    def _summaries(self) -> dict[str, str]:
        """Pass 1 analyses of the whole mailbox, not only of this run's targets, so that
        --match/--limit/--days do not change what pass 2 sees (nor its cache keys)."""
        known = {**self.known, **self.first}
        return {sha: v.summary for sha, v in known.items() if v.summary and not v.error}

    def prescan(self) -> None:
        """Put every verdict already in the cache into the report before any model call.

        A restarted run then starts from the full report instead of an empty one,
        and the report always matches the cache.
        """
        assert self.box is not None
        targets = {m.sha256 for m in self.targets}
        for m in self.box.mails:
            if self.box.is_mine(m):
                continue
            v, cached = self.analyze(m, deep=False, cache_only=True)
            if not cached:
                continue
            if m.sha256 in targets:
                self.first[m.sha256] = self.verdicts[m.sha256] = v
            else:
                self.known[m.sha256] = v  # context for the related mails only
        reviewed = 0
        if self.o.deep:
            summaries = self._summaries()
            for m in self._candidates():
                first = self.first[m.sha256]
                v, cached = self.analyze(m, deep=True, summaries=summaries,
                                         first=None if first.error else first, cache_only=True)
                if cached and not v.error:
                    self.verdicts[m.sha256] = v
                    reviewed += 1
        self.p.log(f"  välimuistissa valmiina: {len(self.first)}/{len(self.targets)} analyysia, {reviewed} tarkistusta")
        self.write(done=False, force=True)

    def run(self) -> int:
        check_target(self.o.target)
        if self.box is None:
            self.load()
        n = len(self.targets)
        t_start = time.monotonic()
        times: list[float] = []
        dtimes: list[float] = []
        try:
            self.prescan()
            mode = "thinking päällä" if self.o.think else "nopea, ilman thinkingiä"
            todo = [m for m in self.targets if m.sha256 not in self.first]
            self.p.log(f"Vaihe 1/2: analyysi {len(todo)} uudelle viestille ({self.llm.model}, {mode})")
            self._warm_pending = True
            for i, m in enumerate(todo, 1):
                v, cached = self.analyze(m, deep=False)
                self.first[m.sha256] = self.verdicts[m.sha256] = v
                s = self._settled(m, v)
                if not cached and not v.error:
                    times.append(v.seconds)
                timing = ("välimuisti" if cached else f"{v.seconds:.0f}s") + self._eta(times, len(todo) - i)
                self.p.log(f"[{i}/{len(todo)}] {m.date_str()[:10]} {(m.from_name or m.from_addr)[:28]} — {m.subject[:50]}\n"
                           f"        → {_short(s)} ({timing})")
                self.write(done=False)
            if self.o.deep:
                summaries = self._summaries()
                flagged = self._candidates()
                self.p.log(f"Vaihe 2/2: tarkistus (thinking + liittyvien viestien analyysit) {len(flagged)} viestille")
                self._warm_pending = True
                self.write(done=False, force=True)
                for i, m in enumerate(flagged, 1):
                    first = self.first[m.sha256]
                    v, cached = self.analyze(m, deep=True, summaries=summaries, first=None if first.error else first)
                    if v.error:
                        self.p.log(f"  tarkistus epäonnistui, käytetään vaiheen 1 tulosta: {v.error[:100]}")
                        self.verdicts[m.sha256] = first
                    else:
                        self.verdicts[m.sha256] = v
                    if cached:
                        continue  # already in the report since the prescan
                    s = self._settled(m, self.verdicts[m.sha256])
                    if not v.error:
                        dtimes.append(v.seconds)
                    timing = f"{v.seconds:.0f}s" + self._eta(dtimes, len(flagged) - i)
                    self.p.log(f"[tarkistus {i}/{len(flagged)}] {m.date_str()[:10]} {(m.from_name or m.from_addr)[:28]} — {m.subject[:50]}\n"
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
