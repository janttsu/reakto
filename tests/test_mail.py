from __future__ import annotations

from datetime import date

from conftest import make_eml
from reakto import prompts
from reakto.mail import clean_text, find_mail_files, html_to_text, read_mail
from reakto.mailbox import Mailbox, detect_my_addresses, normalize_subject, reference_numbers


def test_read_mail_headers_and_signals(maildir):
    p = make_eml(
        maildir, "a", sender="Shop <no-reply@shop.example>",
        headers={"List-Unsubscribe": "<mailto:u@shop.example>", "Authentication-Results": "mx; spf=pass; dkim=fail; dmarc=fail"},
    )
    m = read_mail(p)
    assert m.from_addr == "no-reply@shop.example"
    assert m.signals["list_unsubscribe"] == "yes"
    assert m.signals["noreply_sender"] == "yes"
    assert "dmarc=fail" in m.signals["authentication"]


def test_person_has_no_bulk_signals(maildir):
    m = read_mail(make_eml(maildir, "p", sender="Maija Meikäläinen <maija@gmail.com>"))
    assert "list_unsubscribe" not in m.signals and "noreply_sender" not in m.signals


def test_html_and_urls_are_cleaned():
    text = html_to_text("<style>x{}</style><p>Hei&nbsp;Johannes</p><a href='https://t.example/abc?x=1'>https://t.example/abc?x=1</a>")
    out = clean_text(text, 1000)
    assert "Hei" in out and "Johannes" in out and "t.example" not in out and "[linkki]" in out
    assert clean_text("a" * 50, 10).endswith("…[katkaistu]")


def test_find_mail_files_skips_hidden_and_non_mail(maildir):
    make_eml(maildir, "x")
    (maildir / "notes.txt").write_text("hello")
    hidden = maildir / ".cache"
    hidden.mkdir()
    make_eml(hidden, "y")
    maildir_msg = maildir / "1695000000.M1P2.host"
    maildir_msg.write_bytes((maildir / "x.eml").read_bytes())
    names = sorted(p.name for p in find_mail_files(maildir))
    assert names == ["1695000000.M1P2.host", "x.eml"]


def test_my_addresses_threads_and_replies(maildir):
    first = make_eml(maildir, "q", sender="Maija <maija@gmail.com>", subject="Mökkiviikonloppu?",
                     msgid="<q1@gmail.com>")
    make_eml(maildir, "r", sender="Me <me@example.org>", to="maija@gmail.com", subject="Re: Mökkiviikonloppu?",
             msgid="<r1@example.org>", in_reply_to="<q1@gmail.com>", date="Fri, 04 Sep 2026 10:00:00 +0300")
    for i in range(3):
        make_eml(maildir, f"n{i}", subject=f"Uutiskirje {i}")
    mails = [read_mail(p) for p in find_mail_files(maildir)]
    me = detect_my_addresses(mails)
    assert me == {"me@example.org"}
    box = Mailbox.build(mails, me)
    q = next(m for m in mails if m.path == first)
    assert [r.path.name for r in box.my_replies_after(q)] == ["r.eml"]
    text = prompts.user_message(q, box, date(2026, 9, 29), "fi")
    assert "I replied in this conversation after it: 2026-09-04" in text
    assert "[LATER] ME: Re: Mökkiviikonloppu?" in text


def test_order_numbers_link_messages(maildir):
    make_eml(maildir, "o1", subject="Tilaus #53337 on vahvistettu", date="Sat, 05 Sep 2026 10:00:00 +0300")
    make_eml(maildir, "o2", subject="Tilauksen #53337 lähetys on matkalla", date="Mon, 07 Sep 2026 10:00:00 +0300")
    make_eml(maildir, "o3", sender="Other <x@other.example>", subject="Tilaus #53337", date="Mon, 07 Sep 2026 11:00:00 +0300")
    mails = [read_mail(p) for p in find_mail_files(maildir)]
    box = Mailbox.build(mails, set())
    o1 = next(m for m in mails if m.path.name == "o1.eml")
    assert {m.path.name for m in box.thread(o1)} >= {"o1.eml", "o2.eml"}
    assert "[LATER]" in prompts.related_block(o1, box)


def test_subject_helpers():
    assert normalize_subject("Re: VS: [Tresorit Customer Service] Pending request: Unable to login") == "unable to login"
    assert reference_numbers("Tilaus FI288686 ja #10061090, 2026, 17.9.2026") == {"288686", "10061090"}
