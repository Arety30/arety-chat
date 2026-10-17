"""Block replies to no-reply@example.com: anything addressed to it is moved to Trash, unanswered.

Runs every minute from noreply-guard.timer. Credentials are read fresh from the mail MCP's accounts file
(the bridge password rotates on re-auth).
"""
import email
import imaplib
import json
import os
from email.header import decode_header, make_header
from pathlib import Path

ACCOUNTS = Path(os.environ.get("ARETY_MAIL_ACCOUNTS", str(Path.home() / ".config/proton-mail-mcp/accounts.json")))
BLOCKED = os.environ.get("ARETY_NOREPLY", "no-reply@example.com").lower()
HOST, PORT = "127.0.0.1", 1144


def main() -> None:
    accts = json.loads(ACCOUNTS.read_text())
    accts = accts if isinstance(accts, list) else accts.get("accounts", [])
    acct = next(a for a in accts if a.get("email") == BLOCKED)
    conn = imaplib.IMAP4(HOST, PORT)
    conn.login(acct.get("login") or acct["email"], acct["password"])
    try:
        conn.select("INBOX")
        typ, data = conn.uid("search", None, "ALL")
        uids = (data[0] or b"").split()[-200:]
        moved = 0
        for uid in uids:
            typ, msg = conn.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (TO CC DELIVERED-TO X-ORIGINAL-TO)])")
            raw = b"".join(p[1] for p in msg if isinstance(p, tuple))
            hdr = email.message_from_bytes(raw)
            rcpts = " ".join(str(make_header(decode_header(v))) for k in ("To", "Cc", "Delivered-To", "X-Original-To")
                             for v in hdr.get_all(k, []))
            if BLOCKED in rcpts.lower():
                typ, _ = conn.uid("move", uid, "Trash")
                if typ != "OK":
                    conn.uid("copy", uid, "Trash")
                    conn.uid("store", uid, "+FLAGS", "(\\Deleted)")
                    conn.expunge()
                moved += 1
        if moved:
            print(f"moved {moved} message(s) sent to {BLOCKED} to Trash")
    finally:
        conn.logout()


if __name__ == "__main__":
    main()
