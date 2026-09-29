# reakto

**reakto reads a directory of e-mails with an AI model that runs on your own computer and writes a list of the ones that still need your personal reaction.** Unpaid invoices, bookings coming up, orders still on their way, letters waiting in another service, and above all messages that real people wrote to you. Newsletters, receipts and notifications are left out. Nothing is sent to the internet.

A sister project of [sorto](https://github.com/janttsu/sorto), and built on the same local-only model client.

## In short

```bash
ollama pull qwen3.5:9b                                      # then make the 16k tag below
printf 'FROM qwen3.5:9b\nPARAMETER num_ctx 16384\nPARAMETER num_gpu 99\n' > Modelfile.9b-16k
ollama create qwen3.5:9b-16k -f Modelfile.9b-16k
pipx install git+https://github.com/janttsu/reakto.git

reakto ~/mail-export -t ~/reagoi.md          # analyse every mail, write the report
reakto ~/mail-export -t ~/reagoi.md --days 7 # only last week's mail
```

- **Input:** a directory of `.eml` files or Maildir message files, searched recursively. Work and personal mail can be mixed. reakto only reads it.
- **Output:** one Markdown report (`-t`). Each entry has what the mail is, what you should do, the due or event date (with **OVERDUE** when it has passed), and a link to the message file.
- **Private:** the model server must be on this machine; mail content never leaves it.

## What ends up on the list

The report is in Finnish by default (`--lang en` for English) and grouped like this:

| Section | Examples |
| --- | --- |
| 🔴 Very important: real people | A message a person wrote to you personally and you have not answered. **Always critical**, whatever the model thinks of it. |
| ⚠️ Suspicious | Likely phishing: a "tax office" mail from an unrelated domain, failed SPF/DKIM/DMARC, pressure to click or pay. |
| 🟠 Actions | Invoices to pay (amount, reference and due date are read from PDF attachments too), support cases waiting for your answer, letters in OmaPosti or the online bank, things to confirm, parcels to pick up. |
| 📅 Bookings and events | Table, travel and appointment confirmations for today or later. Past ones drop out. |
| 📦 Orders | Every order still open, also subscriptions and service changes: check what was ordered and that it really happens. It drops out when a later mail confirms the delivery. |
| 🔎 Minor checks | Login alerts and similar "check that it was you" notices. |

A table at the top lists everything in priority order, and the end of the report counts the mails that need nothing.

## How it decides

1. **Every mail is read in full:** decoded headers, the text or HTML body (tracking links removed) and the text of up to two attachments (PDF through `pdftotext`, read from memory, never written to disk).
2. **Header signals** tell bulk and automated mail from people: `List-Unsubscribe`, `Precedence`, `Auto-Submitted`, no-reply senders, mailer IDs, and SPF/DKIM/DMARC results.
3. **Mailbox context:** your own addresses are detected from the mail (or given with `--me`). Threads are rebuilt from `Message-ID`/`References`, subjects and order or ticket numbers. The model sees whether you already replied in the thread, and which earlier and **later** mails exist from the same conversation or sender. A later "your parcel was delivered" or "your ticket was solved" closes the matter.
4. **Pass 1, every mail with thinking on:** the local model (default `qwen3.5:9b-16k`) reasons about the mail before it answers with a structured verdict: summary, human or automated, kind, needs action, action, priority, due date, event date, suspicious, superseded. If the reasoning runs past its token budget, that one mail is answered without thinking instead of being skipped.
5. **Pass 2, a second review:** every mail that would end up in the report (needs action, from a person, suspicious, or uncertain) is reviewed once more with thinking on. This time the analyses of the related mails are included as well. `--no-deep` skips this.

`--no-think` makes pass 1 answer without thinking, several times faster but less careful. Pass 2 then also reviews the gray zone: invoices, orders, bookings, support cases, authorities and personal mail that pass 1 judged to need nothing.
6. **Fixed policy on top of the model:**
   - a person's unanswered mail is critical;
   - suspicious mail is always listed;
   - future bookings are always listed;
   - open orders are always listed (low priority);
   - past bookings and superseded mails drop out;
   - overdue invoices stay on the list.

Newest mail is analysed first, and the report is rewritten while the run goes on. Stopping with Ctrl-C keeps a partial report. Verdicts are cached, so the next run only asks the model about new mail or mail whose context changed. `--refresh` asks again.

## Your own rules

`~/.config/reakto/rules.md` holds plain-language rules that the model follows. `reakto --init-rules` creates a commented template. For example:

```markdown
- Invoices from my electricity company are paid automatically; they need no action.
- Everything from example.com is work mail and important.
- Newsletters from my sports club are not important, but its invoices are.
```

## Privacy and safety

- The model URL must resolve to a **loopback address**. Any other host is refused, including LAN machines and cloud APIs. `HTTP(S)_PROXY` is ignored and redirects are not followed.
- The mail directory is never written to. The report cannot be placed inside it.
- The report is written atomically with mode `0600`. An existing file is replaced only if reakto wrote it (it starts with a marker line). Any other file is refused.
- Cached verdicts quote your mail, so they live in `~/.local/state/reakto/cache.sqlite` (directory `0700`, file `0600`).
- Mail content is treated as untrusted data: the prompt tells the model never to follow instructions inside a message.

## Options

| Option | |
| --- | --- |
| `-t FILE` | report to write (required) |
| `--model NAME` | Ollama model, default `qwen3.5:9b-16k`; `qwen3.6:35b-a3b` is slower but more careful |
| `--llm-url URL` | server on this machine; default: the first of `$OLLAMA_HOST`, `:11434`, `:11435` that has the model |
| `--lang fi\|en\|sv` | language of the analyses |
| `--me ADDRESS` | your address (repeatable; addresses receiving a large share of the mail are detected anyway) |
| `--days N`, `--limit N`, `--match TEXT` | analyse only recent mail, the N newest, or files whose name contains TEXT |
| `--no-think` | pass 1 without thinking (fast mode) |
| `--no-deep` | skip pass 2 |
| `--today YYYY-MM-DD` | judge dates as if it were that day |
| `--refresh` | ignore the cache |
| `--check` | check the model server and paths, then stop |

`~/.config/reakto/config.toml` can set `model`, `url`, `language`, `me = [...]`, `rules`, `think`, `deep`, `deep_max_tokens`, `deep_below_confidence` and `temperature`.

## Speed

Measured on an RTX 2060 6 GB with 31 GB RAM, with thinking on:

| model | how it runs | thinking per mail | time per mail |
| --- | --- | --- | --- |
| `qwen3.5:9b-16k` (default) | all on the GPU, about 45 tok/s | 2,300–7,400 tokens | 50–170 s |
| `qwen3.6:35b-a3b` | experts on the CPU, about 20 tok/s | about 2,000 tokens | 60–130 s |

The 9B writes twice as fast but thinks about twice as long, so both take about as long per mail. On the test mails both reached the same verdicts. The 9B fits the GPU entirely and shares the loaded model with [sorto](https://github.com/janttsu/sorto), so the two tools do not keep evicting each other's model. Without thinking (`--no-think`), a mail takes 14–22 s on the 35B.

The first run over a few hundred mails takes most of a night; after that only new mail costs time. The report never starts from zero: at start, reakto fills it with every verdict already in the cache (`~/.local/state/reakto/cache.sqlite`), also verdicts written by another model for the same mail and context, and only then asks the model about the rest. `--refresh` ignores the cache.

Qwen 3.6 is a hybrid model, so llama.cpp cannot reuse a cached prompt prefix. It can only restore a checkpoint taken at the end of an earlier prompt. On Ollama, reakto therefore renders the ChatML prompt itself (`/api/generate`, raw). It warms the model up once with the system prompt plus the fixed start of the user message, and every mail then restores that checkpoint instead of re-reading about 1,700 tokens. That saves about 10 s per mail.

## Development

```bash
make install   # .venv with dev extras
make test
make lint
```
