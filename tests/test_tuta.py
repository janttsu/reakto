from __future__ import annotations

import json
import sys
from pathlib import Path

from reakto.engine import Engine, Options
from reakto.mcp import MCPClient
from reakto.store import Cache
from reakto.tuta import TutaSource
from test_verdict_report import TODAY, FakeLLM

FAKE = f"{sys.executable} {Path(__file__).with_name('fake_mcp_server.py')}"


def test_mcp_client_lists_tools_and_parses_results():
    with MCPClient(FAKE, timeout=30) as c:
        assert "tuta_mail_list" in c.tools
        folders = c.call("tuta_mail_list_folders")
        assert isinstance(folders, list) and folders[0]["type"] == "inbox"
        one = c.call("tuta_mail_read", list_id="ML", mail_id="m1")
        assert one["subject"] == "Special offer"


def test_tuta_source_fetches_inbox_and_sent(monkeypatch):
    src = TutaSource(command=FAKE, timeout=30)
    mails = src.fetch()
    inbox = [m for m in mails if not m.mine]
    sent = [m for m in mails if m.mine]
    assert [m.subject for m in inbox] == ["Invoice for parking space", "Special offer"]
    assert inbox[0].locator == "ML/m2" and inbox[0].attachments[0].filename == "invoice.pdf"
    assert inbox[1].signals.get("unsubscribe_link_in_body") == "yes"
    assert sent[0].from_addr == "me@example.org" and sent[0].to == ["alex@example.net"]


def test_tuta_end_to_end_with_organize(tmp_path, monkeypatch):
    moves = tmp_path / "moves.jsonl"
    monkeypatch.setenv("FAKE_MCP_MOVES", str(moves))
    src = TutaSource(command=FAKE, important="F-imp", newsletters="Newsletters", archive="archive", timeout=30)
    target = tmp_path / "out.md"
    opts = Options(source=src.label(), target=target, model="fake", url="http://127.0.0.1:1/v1", today=TODAY,
                   live=src, organize=True)
    quiet = type("Q", (), {"log": lambda self, msg: None})()
    assert Engine(opts, llm=FakeLLM(), cache=Cache(tmp_path / "c.sqlite"), progress=quiet).run() == 0
    text = target.read_text()
    assert "Tuta `m2`" in text and "Siirretty kansioihin" in text
    done = [json.loads(line) for line in moves.read_text().splitlines()]
    by_mail = {d["mail_id"]: d["target_folder_elem_id"] for d in done}
    assert by_mail == {"m2": "F-imp", "m1": "F-news"}  # the invoice to important, the advert to newsletters


def test_low_priority_can_go_to_archive():
    from reakto.verdict import Verdict

    src = TutaSource(command=FAKE, important="imp", newsletters="news", archive="arch", important_min_priority="medium")
    assert src.destination(Verdict(needs_action=True, priority="low")) == "arch"
    assert src.destination(Verdict(needs_action=True, priority="high")) == "imp"
    assert src.destination(Verdict(needs_action=False, kind="newsletter")) == "news"
    assert src.destination(Verdict(error="x")) == ""
