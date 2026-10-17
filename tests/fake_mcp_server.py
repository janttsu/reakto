"""A tiny stdio MCP server that imitates tutamcp's mail tools with canned data (for tests).

Moves are appended to the file named in $FAKE_MCP_MOVES so a test can inspect them.
"""

from __future__ import annotations

import json
import os
import sys

FOLDERS = [
    {"id": "L-inbox", "folder_elem_id": "F-inbox", "folder_list_id": "FL", "name": "INBOX", "type": "inbox"},
    {"id": "L-sent", "folder_elem_id": "F-sent", "folder_list_id": "FL", "name": "Sent", "type": "sent"},
    {"id": "L-arch", "folder_elem_id": "F-arch", "folder_list_id": "FL", "name": "Archive", "type": "archive"},
    {"id": "L-imp", "folder_elem_id": "F-imp", "folder_list_id": "FL", "name": "Folder-F-imp", "type": "custom"},
    {"id": "L-news", "folder_elem_id": "F-news", "folder_list_id": "FL", "name": "Newsletters", "type": "custom"},
]
INBOX = [
    {"id": "m2", "list_id": "ML", "subject": "Invoice for parking space", "from": "Example Club <club@example.net>",
     "to": "me@example.org", "date": "2027-03-02T10:00:00+00:00", "unread": True, "has_attachments": True, "e2e": False},
    {"id": "m1", "list_id": "ML", "subject": "Special offer", "from": "Shop <news@shop.example>",
     "to": "me@example.org", "date": "2027-03-01T10:00:00+00:00", "unread": False, "has_attachments": False, "e2e": False},
]
SENT = [
    {"id": "s1", "list_id": "SL", "subject": "Re: Weekend plans?", "from": "Me <me@example.org>",
     "to": "=?utf-8?q?Alex_Ex=C3=A4mple?= <alex@example.net>", "date": "2027-03-02T12:00:00+00:00",
     "unread": False, "has_attachments": False, "e2e": False},
]
BODIES = {
    "m2": {"body": "Hello,\n\nthe invoice is attached.\n", "attachments": [{"name": "invoice.pdf", "mime": "application/pdf", "index": 0}]},
    "m1": {"body": "Buy now! Click here to unsubscribe.", "attachments": []},
}


def tool(name: str, args: dict):
    if name == "tuta_mail_list_folders":
        return FOLDERS
    if name == "tuta_mail_list":
        rows = {"L-inbox": INBOX, "L-sent": SENT}.get(args["folder_id"], [])
        return rows[: int(args.get("limit", 20))]
    if name == "tuta_mail_read":
        s = next(m for m in INBOX if m["id"] == args["mail_id"])
        b = BODIES[s["id"]]
        return {**s, "to": [s["to"]], "cc": [], "bcc": [], **b}
    if name == "tuta_mail_get_attachment":
        return {"error": "no attachment data in the fake server"}
    if name == "tuta_mail_move":
        with open(os.environ["FAKE_MCP_MOVES"], "a", encoding="utf-8") as f:
            f.write(json.dumps(args) + "\n")
        return {"status": "moved"}
    return {"error": f"unknown tool {name}"}


def main() -> None:
    tools = [{"name": n, "inputSchema": {"type": "object"}} for n in (
        "tuta_mail_list_folders", "tuta_mail_list", "tuta_mail_read", "tuta_mail_get_attachment", "tuta_mail_move")]
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if "id" not in msg:
            continue  # notification
        method = msg.get("method")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {}, "serverInfo": {"name": "fake", "version": "0"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "tools/call":
            data = tool(msg["params"]["name"], msg["params"].get("arguments") or {})
            # FastMCP style: one text item per list element
            items = data if isinstance(data, list) else [data]
            result = {"content": [{"type": "text", "text": json.dumps(d)} for d in items], "isError": False}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
