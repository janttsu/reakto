from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from conftest import make_eml
from reakto.engine import Engine, Options
from reakto.llm import Completion, NotLocalError, ensure_local_url
from reakto.mail import read_mail
from reakto.report import MARKER, Item, TargetNotOurs, check_target, render
from reakto.store import Cache
from reakto.verdict import Verdict, parse_verdict, settle

TODAY = date(2026, 9, 29)


def test_parse_verdict_normalizes():
    v = parse_verdict('```json\n{"summary": "Lasku", "sender_type": "Human", "kind": "invoice", "needs_action": "true",'
                      ' "action_type": "pay", "priority": "HIGH", "deadline": "17.9.2026", "confidence": 3}\n```')
    assert v.sender_type == "human" and v.needs_action and v.priority == "high"
    assert v.deadline == "2026-09-17" and v.confidence == 1.0
    with pytest.raises(ValueError):
        parse_verdict('{"foo": 1}')


def test_settle_policy():
    human = settle(Verdict(sender_type="human", needs_action=False, priority="low"), replied_after=False, today=TODAY)
    assert human.needs_action and human.priority == "critical" and human.action_type == "reply"
    replied = settle(Verdict(sender_type="human", needs_action=True, priority="high"), replied_after=True, today=TODAY)
    assert not replied.needs_action
    past = settle(Verdict(needs_action=True, action_type="attend", event_date="2026-09-02", priority="low"),
                  replied_after=False, today=TODAY)
    assert not past.needs_action
    overdue = settle(Verdict(needs_action=True, action_type="pay", deadline="2026-09-17", priority="high"),
                     replied_after=False, today=TODAY)
    assert overdue.needs_action
    phish = settle(Verdict(suspicious=True, priority="low"), replied_after=False, today=TODAY)
    assert phish.needs_action and phish.priority == "high" and phish.action_type == "check_security"
    sup = settle(Verdict(needs_action=True, action_type="await_delivery", superseded=True, priority="low"),
                 replied_after=False, today=TODAY)
    assert not sup.needs_action


def test_render_sections_and_overdue(maildir):
    m = read_mail(make_eml(maildir, "lasku", subject="Lasku laituripaikasta"))
    v = Verdict(summary="Laiturimaksu 116,33 €", needs_action=True, action_type="pay", action="Maksa 116,33 €",
                priority="high", deadline="2026-09-17", kind="invoice")
    text = render([Item(m, v)], today=TODAY, source=maildir, model="m", total=1, analyzed=1, done=True)
    assert text.startswith(MARKER)
    assert "Toimenpiteet (1)" in text and "ERÄÄNTYNYT 12 pv sitten" in text and "(myöhässä)" in text


def test_target_guard(tmp_path: Path):
    foreign = tmp_path / "notes.md"
    foreign.write_text("my own notes\n")
    with pytest.raises(TargetNotOurs):
        check_target(foreign)
    ours = tmp_path / "ours.md"
    ours.write_text(MARKER + "\nold\n")
    check_target(ours)


def test_only_loopback_urls():
    assert ensure_local_url("http://127.0.0.1:11434/v1")
    with pytest.raises(NotLocalError):
        ensure_local_url("http://192.168.1.10:11434/v1")
    with pytest.raises(NotLocalError):
        ensure_local_url("https://api.example.com/v1")


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.calls = []

    def complete(self, messages, *, think=False, max_tokens=1200, json_mode=True, temperature=None):
        user = messages[-1]["content"]
        self.calls.append((think, user))
        if "laituri" in user.lower():
            d = {"summary": "Lasku", "sender_type": "automated", "kind": "invoice", "needs_action": True,
                 "action_type": "pay", "action": "Maksa", "priority": "high", "deadline": "2026-09-17",
                 "confidence": 0.9, "reason": "lasku"}
        else:
            d = {"summary": "Mainos", "sender_type": "automated", "kind": "marketing", "needs_action": False,
                 "action_type": "none", "priority": "none", "confidence": 0.95, "reason": "mainos"}
        return Completion(content=json.dumps(d), reasoning="", seconds=0.01)

    def complete_chat(self, system, user, *, think=False, max_tokens=1200, temperature=None):
        return self.complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                             think=think, max_tokens=max_tokens)


def test_engine_end_to_end_with_cache(maildir, tmp_path):
    make_eml(maildir, "lasku", sender="Seura <seura@gmail.com>", subject="Lasku laituripaikasta")
    make_eml(maildir, "ad", subject="Tarjous", headers={"List-Unsubscribe": "<mailto:x@example.com>"})
    target = tmp_path / "out.md"
    opts = Options(source=maildir, target=target, model="fake", url="http://127.0.0.1:1/v1", today=TODAY)
    llm = FakeLLM()
    cache = Cache(tmp_path / "c.sqlite")
    quiet = type("Q", (), {"log": lambda self, msg: None})()
    assert Engine(opts, llm=llm, cache=cache, progress=quiet).run() == 0
    # thinking for every mail, then a second thinking review of the invoice only
    assert [think for think, _ in llm.calls] == [True, True, True]
    text = target.read_text()
    assert "Lasku laituripaikasta" in text and "Tarjous" not in text.split("## Ei vaadi")[0]
    llm2 = FakeLLM()
    assert Engine(opts, llm=llm2, cache=cache, progress=quiet).run() == 0
    assert llm2.calls == []  # everything came from the cache


def test_future_booking_is_always_listed():
    v = settle(Verdict(kind="booking", needs_action=False, action_type="none", event_date="2026-10-09",
                       priority="none"), replied_after=False, today=TODAY)
    assert v.needs_action and v.action_type == "attend" and v.priority == "low"
    soon = settle(Verdict(kind="booking", needs_action=False, event_date="2026-09-30"), replied_after=False, today=TODAY)
    assert soon.priority == "high"
    past = settle(Verdict(kind="ticket_or_trip", needs_action=True, action_type="attend", event_date="2026-09-02",
                          priority="low"), replied_after=False, today=TODAY)
    assert not past.needs_action


def test_fold_threads_keeps_newest_and_escalates(maildir):
    from reakto.report import fold_threads

    old = read_mail(make_eml(maildir, "t1", subject="Re: Unable to login", date="Thu, 17 Sep 2026 10:00:00 +0300"))
    new = read_mail(make_eml(maildir, "t2", subject="Pending request: Unable to login",
                             date="Sat, 26 Sep 2026 10:00:00 +0300"))
    items = [
        Item(old, Verdict(needs_action=True, action_type="reply", priority="high", sender_type="human")),
        Item(new, Verdict(needs_action=True, action_type="reply", priority="high")),
    ]
    out = fold_threads(items, {old.sha256: "T", new.sha256: "T"}, {"T"})
    heads = [i for i in out if i.verdict.needs_action]
    assert len(heads) == 1 and heads[0].mail is new and heads[0].also == [old]
    assert heads[0].verdict.priority == "critical"
    text = render(out, today=TODAY, source=maildir, model="m", total=2, analyzed=2, done=True)
    assert "Samassa ketjussa myös (1)" in text


def test_engine_no_think_reviews_gray_zone(maildir, tmp_path):
    make_eml(maildir, "lasku", sender="Seura <seura@gmail.com>", subject="Lasku laituripaikasta")
    make_eml(maildir, "ad", subject="Tarjous", headers={"List-Unsubscribe": "<mailto:x@example.com>"})
    opts = Options(source=maildir, target=tmp_path / "out.md", model="fake", url="http://127.0.0.1:1/v1",
                   today=TODAY, think=False)
    llm = FakeLLM()
    quiet = type("Q", (), {"log": lambda self, msg: None})()
    assert Engine(opts, llm=llm, cache=Cache(tmp_path / "c.sqlite"), progress=quiet).run() == 0
    assert [think for think, _ in llm.calls] == [False, False, True]
