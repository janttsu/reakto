"""reakto SOURCE -t TARGET: list the e-mails that still need my personal reaction."""

from __future__ import annotations

import argparse
import os
import re
import sys
import tomllib
from datetime import date
from pathlib import Path

from reakto import __version__
from reakto.llm import LocalLLM, NotLocalError, probe_url
from reakto.report import TargetNotOurs, check_target

DEFAULT_MODEL = "qwen3.6:35b-a3b"
DEFAULT_URLS = ["http://127.0.0.1:11434/v1", "http://127.0.0.1:11435/v1"]

RULES_TEMPLATE = """\
<!-- reakto rules: plain-language rules for the local model, in any language.
     Text inside these comment markers is ignored. Examples:
- Mails from my employer example.com are work mail; treat them as important.
- Newsletters from my sports club are not important, but its invoices are.
- I pay the electricity bill automatically; its invoices need no action.
-->
"""


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "reakto"


def load_config(path: Path | None) -> dict:
    p = path or config_dir() / "config.toml"
    if not p.exists():
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def read_rules(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def _url_candidates() -> list[str]:
    urls = []
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if host:
        if "://" not in host:
            host = "http://" + host
        urls.append(host.rstrip("/") + "/v1")
    return urls + [u for u in DEFAULT_URLS if u not in urls]


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="reakto",
        description="Analyse a directory of e-mails (.eml / Maildir) with a LOCAL language model "
        "and list the ones that still need your personal reaction.",
    )
    ap.add_argument("source", nargs="?", type=Path, help="directory with the e-mail files (read only)")
    ap.add_argument("-t", "--target", type=Path, help="Markdown report to write")
    ap.add_argument("--model", help=f"Ollama model (default {DEFAULT_MODEL})")
    ap.add_argument("--llm-url", help="OpenAI-compatible URL on this machine (default: probe Ollama on :11434, :11435)")
    ap.add_argument("--lang", default=None, help="language of the analyses: fi (default), en, sv")
    ap.add_argument("--me", action="append", default=[], help="my e-mail address (repeatable; also auto-detected)")
    ap.add_argument("--rules", type=Path, help=f"rules file (default {config_dir() / 'rules.md'})")
    ap.add_argument("--config", type=Path, help=f"config file (default {config_dir() / 'config.toml'})")
    ap.add_argument("--no-deep", action="store_true", help="skip the second, thinking pass")
    ap.add_argument("--days", type=int, help="only mails from the last N days")
    ap.add_argument("--limit", type=int, help="only the N newest mails (testing)")
    ap.add_argument("--match", action="append", default=[], help="only files whose name contains this text (repeatable)")
    ap.add_argument("--today", type=date.fromisoformat, help="pretend today is YYYY-MM-DD")
    ap.add_argument("--refresh", action="store_true", help="ignore cached verdicts and ask the model again")
    ap.add_argument("--check", action="store_true", help="only check the model server and the paths")
    ap.add_argument("--init-rules", action="store_true", help="create a commented rules.md template")
    ap.add_argument("--version", action="version", version=f"reakto {__version__}")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = load_config(args.config)
    rules_path = args.rules or Path(os.path.expanduser(cfg.get("rules", str(config_dir() / "rules.md"))))
    if args.init_rules:
        if rules_path.exists():
            print(f"{rules_path} exists already; not touching it.")
        else:
            rules_path.parent.mkdir(parents=True, exist_ok=True)
            with open(rules_path, "x", encoding="utf-8") as f:
                f.write(RULES_TEMPLATE)
            print(f"created {rules_path}")
        return 0
    if args.source is None or args.target is None:
        build_parser().error("SOURCE and -t TARGET are required")
    source = args.source.expanduser().resolve()
    target = args.target.expanduser().resolve()
    if not source.is_dir():
        print(f"reakto: {source} is not a directory", file=sys.stderr)
        return 2
    try:
        target.relative_to(source)
        print("reakto: the report must not be written inside the mail directory", file=sys.stderr)
        return 2
    except ValueError:
        pass
    try:
        check_target(target)
    except TargetNotOurs as e:
        print(f"reakto: {e}", file=sys.stderr)
        return 2

    model = args.model or cfg.get("model", DEFAULT_MODEL)
    url = args.llm_url or cfg.get("url")
    try:
        if url:
            ok, msg = LocalLLM(base_url=url, model=model, max_retries=0).health()
            if not ok:
                print(f"reakto: {msg}", file=sys.stderr)
                return 2
        else:
            url = probe_url(_url_candidates(), model)
            if not url:
                print(f"reakto: no local Ollama with model {model!r} found at {', '.join(_url_candidates())}. "
                      f"Pull it (ollama pull {model}) or give --llm-url.", file=sys.stderr)
                return 2
    except NotLocalError as e:
        print(f"reakto: {e}", file=sys.stderr)
        return 2
    rules = read_rules(rules_path)
    print(f"reakto {__version__}: model {model} at {url} (local only)"
          + (f", {len(rules.splitlines())} rule line(s) from {rules_path}" if rules else ""), file=sys.stderr)
    if args.check:
        return 0

    from reakto.engine import Engine, Options

    opts = Options(
        source=source,
        target=target,
        model=model,
        url=url,
        language=args.lang or cfg.get("language", "fi"),
        rules=rules,
        me=args.me + list(cfg.get("me", [])),
        deep=not args.no_deep and bool(cfg.get("deep", True)),
        deep_max_tokens=int(cfg.get("deep_max_tokens", 8192)),
        min_confidence=float(cfg.get("deep_below_confidence", 0.75)),
        since_days=args.days,
        limit=args.limit,
        match=args.match,
        refresh=args.refresh,
        today=args.today or date.today(),
        temperature=float(cfg.get("temperature", 0.3)),
    )
    return Engine(opts).run()


if __name__ == "__main__":
    sys.exit(main())
