"""Accounts for Arety Chat: sign-up / login / password reset by email, saved chats, personal API keys.

Everything lives in one SQLite file. Passwords are scrypt-hashed; session, reset and API tokens are
stored only as SHA-256 hashes, so a copy of the database does not let anyone in.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Dict, List, Optional

SESSION_DAYS = 30
RESET_MINUTES = 60
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE NOT NULL, name TEXT,
    pw TEXT NOT NULL, created REAL, last_login REAL);
CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL, expires REAL);
CREATE TABLE IF NOT EXISTS resets (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL, used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS chats (id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT, model TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS ix_chats_owner ON chats(owner, updated);
CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, role TEXT, text TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_messages_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS api_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT, key TEXT UNIQUE,
    prefix TEXT, created REAL, last_used REAL, revoked INTEGER DEFAULT 0);
"""


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${dk.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, dk = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(dk) // 2)
        return hmac.compare_digest(got.hex(), dk)
    except Exception:  # noqa: BLE001
        return False


def password_problem(pw: str) -> Optional[str]:
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if len(pw) > 200:
        return "Password is too long."
    return None


class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit, self.window = limit, window
        self.hits: Dict[str, List[float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            h = [t for t in self.hits.get(key, []) if now - t < self.window]
            if len(h) >= self.limit:
                self.hits[key] = h
                return False
            h.append(now)
            self.hits[key] = h
            if len(self.hits) > 20000:
                self.hits = {k: v for k, v in self.hits.items() if v and now - v[-1] < self.window}
            return True


class Accounts:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()
        os.chmod(self.path, 0o600)

    def _x(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def _one(self, sql: str, args: tuple = ()) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> List[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ------------------------------------------------------------ users ----
    def create_user(self, email: str, password: str, name: str = "") -> Dict[str, Any]:
        email = email.strip().lower()
        if not EMAIL_RE.match(email):
            raise ValueError("Enter a valid email address.")
        prob = password_problem(password)
        if prob:
            raise ValueError(prob)
        if self._one("SELECT 1 FROM users WHERE email=?", (email,)):
            raise ValueError("An account with that email already exists. Try signing in.")
        cur = self._x("INSERT INTO users (email, name, pw, created) VALUES (?,?,?,?)",
                      (email, name.strip()[:80], hash_password(password), time.time()))
        return self.user(cur.lastrowid)

    def user(self, uid: int) -> Optional[Dict[str, Any]]:
        r = self._one("SELECT id, email, name, created FROM users WHERE id=?", (uid,))
        return dict(r) if r else None

    def authenticate(self, email: str, password: str) -> Optional[Dict[str, Any]]:
        r = self._one("SELECT id, pw FROM users WHERE email=?", (email.strip().lower(),))
        if r is None:
            check_password(password, hash_password("timing-equaliser"))   # same work either way
            return None
        if not check_password(password, r["pw"]):
            return None
        self._x("UPDATE users SET last_login=? WHERE id=?", (time.time(), r["id"]))
        return self.user(r["id"])

    def set_password(self, uid: int, password: str) -> None:
        prob = password_problem(password)
        if prob:
            raise ValueError(prob)
        self._x("UPDATE users SET pw=? WHERE id=?", (hash_password(password), uid))
        self._x("DELETE FROM sessions WHERE user_id=?", (uid,))       # signed out everywhere

    # --------------------------------------------------------- sessions ----
    def new_session(self, uid: int) -> str:
        tok = secrets.token_urlsafe(32)
        now = time.time()
        self._x("INSERT INTO sessions (token, user_id, created, expires) VALUES (?,?,?,?)", (_h(tok), uid, now, now + SESSION_DAYS * 86400))
        return tok

    def session_user(self, tok: Optional[str]) -> Optional[Dict[str, Any]]:
        if not tok or len(tok) > 100:
            return None
        r = self._one("SELECT user_id, expires FROM sessions WHERE token=?", (_h(tok),))
        if r is None or r["expires"] < time.time():
            return None
        return self.user(r["user_id"])

    def end_session(self, tok: Optional[str]) -> None:
        if tok:
            self._x("DELETE FROM sessions WHERE token=?", (_h(tok),))

    # ----------------------------------------------------------- resets ----
    def new_reset(self, email: str) -> Optional[tuple]:
        r = self._one("SELECT id, email, name FROM users WHERE email=?", (email.strip().lower(),))
        if r is None:
            return None
        tok = secrets.token_urlsafe(32)
        self._x("INSERT INTO resets (token, user_id, expires) VALUES (?,?,?)", (_h(tok), r["id"], time.time() + RESET_MINUTES * 60))
        return tok, dict(r)

    def use_reset(self, tok: str, password: str) -> Dict[str, Any]:
        r = self._one("SELECT user_id, expires, used FROM resets WHERE token=?", (_h(tok or ""),))
        if r is None or r["used"] or r["expires"] < time.time():
            raise ValueError("This reset link is invalid or has expired. Ask for a new one.")
        self.set_password(r["user_id"], password)
        self._x("UPDATE resets SET used=1 WHERE user_id=?", (r["user_id"],))     # every outstanding link dies
        return self.user(r["user_id"])

    # ------------------------------------------------------------ chats ----
    def new_chat(self, owner: str, model: str, title: str = "New chat") -> Dict[str, Any]:
        cid = secrets.token_hex(8)
        now = time.time()
        self._x("INSERT INTO chats (id, owner, title, model, created, updated) VALUES (?,?,?,?,?,?)", (cid, owner, title, model, now, now))
        return {"id": cid, "title": title, "model": model, "created": now, "updated": now}

    def chat(self, owner: str, cid: str) -> Optional[Dict[str, Any]]:
        r = self._one("SELECT id, title, model, created, updated FROM chats WHERE id=? AND owner=?", (cid, owner))
        return dict(r) if r else None

    def chats(self, owner: str, limit: int = 200) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT id, title, model, created, updated FROM chats WHERE owner=? ORDER BY updated DESC LIMIT ?",
                                           (owner, limit))]

    def messages(self, cid: str, limit: int = 500) -> List[Dict[str, Any]]:
        rows = self._all("SELECT role, text, ts FROM messages WHERE chat_id=? ORDER BY id DESC LIMIT ?", (cid, limit))
        return [dict(r) for r in reversed(rows)]

    def add_messages(self, cid: str, turns: List[tuple]) -> None:
        now = time.time()
        with self.lock:
            self.db.executemany("INSERT INTO messages (chat_id, role, text, ts) VALUES (?,?,?,?)", [(cid, r, t, ts) for r, t, ts in turns])
            self.db.execute("UPDATE chats SET updated=? WHERE id=?", (now, cid))
            self.db.commit()

    def rename_chat(self, owner: str, cid: str, title: str) -> bool:
        return self._x("UPDATE chats SET title=? WHERE id=? AND owner=?", (title.strip()[:120] or "Untitled", cid, owner)).rowcount > 0

    def delete_chat(self, owner: str, cid: str) -> bool:
        if self._x("DELETE FROM chats WHERE id=? AND owner=?", (cid, owner)).rowcount == 0:
            return False
        self._x("DELETE FROM messages WHERE chat_id=?", (cid,))
        return True

    def adopt_guest_chats(self, guest_owner: str, owner: str) -> int:
        """Chats started before signing in move into the account."""
        return self._x("UPDATE chats SET owner=? WHERE owner=?", (owner, guest_owner)).rowcount

    def forget_old_guests(self, days: int = 14) -> None:
        cut = time.time() - days * 86400
        ids = [r["id"] for r in self._all("SELECT id FROM chats WHERE owner LIKE 'g.%' AND updated<?", (cut,))]
        for cid in ids:
            self._x("DELETE FROM messages WHERE chat_id=?", (cid,))
            self._x("DELETE FROM chats WHERE id=?", (cid,))

    # --------------------------------------------------------- api keys ----
    def new_api_key(self, uid: int, name: str = "") -> Dict[str, Any]:
        if (self._one("SELECT COUNT(*) AS n FROM api_keys WHERE user_id=? AND revoked=0", (uid,)) or {"n": 0})["n"] >= 10:
            raise ValueError("You already have 10 keys. Revoke one first.")
        key = "ak_" + secrets.token_urlsafe(30)
        cur = self._x("INSERT INTO api_keys (user_id, name, key, prefix, created) VALUES (?,?,?,?,?)",
                      (uid, name.strip()[:60] or "key", _h(key), key[:10], time.time()))
        return {"id": cur.lastrowid, "name": name.strip()[:60] or "key", "key": key, "prefix": key[:10]}

    def api_keys(self, uid: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self._all("SELECT id, name, prefix, created, last_used FROM api_keys WHERE user_id=? AND revoked=0 ORDER BY id",
                                           (uid,))]

    def revoke_api_key(self, uid: int, kid: int) -> bool:
        return self._x("UPDATE api_keys SET revoked=1 WHERE id=? AND user_id=?", (kid, uid)).rowcount > 0

    def resolve_api_key(self, key: str) -> Optional[str]:
        """Personal API key -> the account it acts as ("u<id>"), so API calls share that person's memory."""
        if not key.startswith("ak_") or len(key) > 80:
            return None
        r = self._one("SELECT id, user_id FROM api_keys WHERE key=? AND revoked=0", (_h(key),))
        if r is None:
            return None
        self._x("UPDATE api_keys SET last_used=? WHERE id=?", (time.time(), r["id"]))
        return f"u{r['user_id']}"


# ------------------------------------------------------------------ mail ----
MAIL_ACCOUNTS = Path(os.environ.get("ARETY_MAIL_ACCOUNTS", str(Path.home() / ".config/proton-mail-mcp/accounts.json")))
MAIL_FROM = os.environ.get("ARETY_MAIL_FROM", "no-reply@example.com")


def send_mail(to: str, subject: str, text: str, html: str = "") -> None:
    """Send through the local Proton bridge (credentials read fresh each time: the bridge password rotates on re-auth)."""
    accts = json.loads(MAIL_ACCOUNTS.read_text())
    accts = accts if isinstance(accts, list) else accts.get("accounts", [])
    acct = next(a for a in accts if a.get("email") == MAIL_FROM)
    msg = EmailMessage()
    msg["From"] = f"Arety <{MAIL_FROM}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    host, port = os.environ.get("ARETY_SMTP_HOST", "127.0.0.1"), int(os.environ.get("ARETY_SMTP_PORT", "1026"))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.login(acct.get("login") or acct["email"], acct["password"])
        s.send_message(msg)
