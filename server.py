"""Arety Chat — text-only chat interface (https://example.com).

The public API exposes only model names and answers. Internals stay server-side.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from rvtcb.brain import LEGACY_LAYOUT, SERVING_LAYOUT, Brain

from accounts import Accounts, RateLimiter, send_mail

HERE = Path(__file__).resolve().parent
DATA = Path(os.environ.get("ARETY_CHAT_DATA", HERE / "data"))
DATA.mkdir(parents=True, exist_ok=True)

COOKIE_SID = "ac_sid"
SESSION_TTL = 6 * 3600

# ----------------------------------------------------------------- models ----
# A "model" is a brain configuration: our own processors in every node, no outside language model.
# One brain per model, many conversations (sessions) inside it.
MODELS: Dict[str, Dict[str, Any]] = {
    "arety-rcb-1.0": {
        "name": "Arety RCB 1.0",
        "description": "Arety RCB 1.0 — text only.",
        "layout": LEGACY_LAYOUT,
        "consensus": False,
        "web": False,
    },
    "arety-rcb-1.2": {
        "name": "Arety RCB 1.2",
        "description": "Arety RCB 1.2 (RVTCGI) — text only.",
        "layout": SERVING_LAYOUT,
        "consensus": True,
        "web": False,
    },
}
DEFAULT_MODEL = "arety-rcb-1.2"
KNOWLEDGE_DIR = os.environ.get("ARETY_KNOWLEDGE_DIR") or None
DICTIONARY = os.environ.get("ARETY_DICTIONARY") or None      # multilingual dictionary TSV (built on Kaggle)
KNOWLEDGE_DB = os.environ.get("ARETY_KNOWLEDGE_DB") or None  # whole-of-Wikidata SQLite (built in the sandbox)
GENERATOR = os.environ.get("ARETY_GENERATOR") or None        # our own transformer weights (trained in a notebook)
ENCYCLOPEDIA = os.environ.get("ARETY_ENCYCLOPEDIA") or None  # Wikipedia summaries (notebook 7)

_brains: Dict[str, Brain] = {}
_brains_lock = threading.Lock()
def get_brain(model_id: str) -> Brain:
    with _brains_lock:
        b = _brains.get(model_id)
        if b is None:
            cfg = MODELS[model_id]
            b = Brain(layout=cfg["layout"], web=cfg["web"], prefer_native=True, model_name=model_id, consensus=cfg["consensus"],
                      data_dir=str(DATA / model_id), knowledge_dir=KNOWLEDGE_DIR, dictionary_path=DICTIONARY,
                      knowledge_db=KNOWLEDGE_DB, generator_path=GENERATOR,
                      encyclopedia_db=ENCYCLOPEDIA)
            _brains[model_id] = b
        return b


_endpoint_servers: List[Any] = []


def start_endpoints() -> None:
    """Expose the brain's two special nodes over HTTP (localhost; Apache fronts them)."""
    b = get_brain(DEFAULT_MODEL)
    api_keys = [k for k in os.environ.get("ARETY_API_KEYS", "").split(",") if k.strip()]
    train_keys = [k for k in os.environ.get("ARETY_TRAIN_KEYS", "").split(",") if k.strip()] or api_keys
    api = b.endpoint("api")
    tr = b.endpoint("training")
    if api is not None:
        api.key_resolver = accounts.resolve_api_key       # personal keys act as their account (memory + context)
    if api is not None:
        _endpoint_servers.append(api.serve("127.0.0.1", int(os.environ.get("ARETY_API_PORT", "8003")), api_keys))
        _log.info("api endpoint node on %s", _endpoint_servers[-1].url)
    if tr is not None:
        _endpoint_servers.append(tr.serve("127.0.0.1", int(os.environ.get("ARETY_TRAIN_PORT", "8004")), train_keys))
        _log.info("training endpoint node on %s", _endpoint_servers[-1].url)


def get_sid(request: Request, response: Response) -> str:
    sid = request.cookies.get(COOKIE_SID)
    if not sid or not sid.isalnum() or len(sid) != 32:
        sid = secrets.token_hex(16)
        request.state.new_sid = sid
        response.set_cookie(COOKIE_SID, sid, httponly=True, samesite="lax", secure=True, max_age=SESSION_TTL)
    return sid


# --------------------------------------------------------------- accounts ----
COOKIE_AUTH = "ac_auth"
PUBLIC_URL = os.environ.get("ARETY_PUBLIC_URL", "https://example.com").rstrip("/")
accounts = Accounts(DATA / "accounts.sqlite")
_login_rl, _signup_rl = RateLimiter(10, 900), RateLimiter(5, 3600)
_forgot_rl_email, _forgot_rl_ip, _chat_rl = RateLimiter(3, 3600), RateLimiter(10, 3600), RateLimiter(60, 60)


def client_ip(request: Request) -> str:
    return (request.headers.get("x-arety-client-ip") or request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else "?"))


def current_user(request: Request) -> Optional[Dict[str, Any]]:
    return accounts.session_user(request.cookies.get(COOKIE_AUTH))


def owner_of(request: Request, response: Response) -> str:
    """Who owns chats/memory for this request: the account, or this browser while signed out."""
    u = current_user(request)
    return f"u{u['id']}" if u else "g." + get_sid(request, response)


def _sign_in(request: Request, response: Response, user: Dict[str, Any]) -> None:
    tok = accounts.new_session(user["id"])
    response.set_cookie(COOKIE_AUTH, tok, httponly=True, samesite="lax", secure=True, max_age=30 * 86400)
    sid = request.cookies.get(COOKIE_SID)
    if sid and sid.isalnum() and len(sid) == 32:
        accounts.adopt_guest_chats("g." + sid, f"u{user['id']}")      # chats from before signing in come along
        for b in list(_brains.values()):                                # and so does what Arety learned about them
            b.merge_user("g." + sid, f"u{user['id']}")


def public_user(u: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return {"email": u["email"], "name": u.get("name") or u["email"].split("@")[0]} if u else None


# ------------------------------------------------------------------- app ----
_log = logging.getLogger("arety-chat")
app = FastAPI(title="Arety Chat", version="0.2", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def same_origin_writes(request: Request, call_next):
    """Cookie-authenticated writes must come from this site (no cross-site form posts)."""
    if request.method in ("POST", "PATCH", "PUT", "DELETE") and request.url.path.startswith("/api/"):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in (PUBLIC_URL, f"http://127.0.0.1:{os.environ.get('PORT', '8002')}"):
            return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
    return await call_next(request)


@app.get("/api/me")
def me(request: Request):
    return {"user": public_user(current_user(request)), "default_model": DEFAULT_MODEL}


class SignupBody(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginBody(BaseModel):
    email: str
    password: str


class EmailBody(BaseModel):
    email: str


class ResetBody(BaseModel):
    token: str
    password: str


class PasswordBody(BaseModel):
    current: str
    password: str


@app.post("/api/signup")
def signup(body: SignupBody, request: Request, response: Response):
    if not _signup_rl.allow(client_ip(request)):
        raise HTTPException(429, "Too many sign-ups from here. Try again later.")
    try:
        u = accounts.create_user(body.email, body.password, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _sign_in(request, response, u)
    _log.info("signup user=%s", u["id"])
    return {"user": public_user(u)}


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    key = client_ip(request) + "|" + body.email.strip().lower()
    if not _login_rl.allow(key):
        raise HTTPException(429, "Too many attempts. Wait 15 minutes and try again.")
    u = accounts.authenticate(body.email, body.password)
    if u is None:
        raise HTTPException(401, "Wrong email or password.")
    _sign_in(request, response, u)
    return {"user": public_user(u)}


@app.post("/api/logout")
def logout(request: Request, response: Response):
    accounts.end_session(request.cookies.get(COOKIE_AUTH))
    response.delete_cookie(COOKIE_AUTH)
    return {"ok": True}


def _send_reset(email: str) -> None:
    got = accounts.new_reset(email)
    if got is None:
        return
    tok, u = got
    link = f"{PUBLIC_URL}/?reset={tok}"
    name = u.get("name") or "there"
    text = (f"Hi {name},\n\nSomeone asked to reset the password for your Arety account ({u['email']}).\n"
            f"Open this link within 60 minutes to choose a new password:\n\n{link}\n\n"
            "If it wasn't you, ignore this email — your password stays the same.\n\n— Arety")
    html = (f"<p>Hi {name},</p><p>Someone asked to reset the password for your Arety account (<b>{u['email']}</b>).</p>"
            f"<p><a href=\"{link}\" style=\"display:inline-block;padding:10px 16px;background:#4da3ff;color:#061020;"
            f"border-radius:8px;text-decoration:none;font-weight:700\">Choose a new password</a></p>"
            "<p>The link works once and expires in 60 minutes. If it wasn't you, ignore this email.</p><p>— Arety</p>")
    try:
        send_mail(u["email"], "Reset your Arety password", text, html)
        _log.info("reset mail sent user=%s", u["id"])
    except Exception as e:  # noqa: BLE001
        _log.error("reset mail failed user=%s: %s", u["id"], e)


@app.post("/api/forgot")
def forgot(body: EmailBody, request: Request):
    email = body.email.strip().lower()
    if _forgot_rl_ip.allow(client_ip(request)) and _forgot_rl_email.allow(email):
        threading.Thread(target=_send_reset, args=(email,), daemon=True).start()
    # same answer whether or not the account exists
    return {"ok": True, "message": "If an account exists for that email, a reset link is on its way."}


@app.post("/api/reset-password")
def reset_password(body: ResetBody, request: Request, response: Response):
    if not _login_rl.allow(client_ip(request) + "|reset"):
        raise HTTPException(429, "Too many attempts. Try again later.")
    try:
        u = accounts.use_reset(body.token, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _sign_in(request, response, u)
    return {"user": public_user(u)}


@app.post("/api/password")
def change_password(body: PasswordBody, request: Request, response: Response):
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "Sign in first.")
    if accounts.authenticate(u["email"], body.current) is None:
        raise HTTPException(400, "Current password is wrong.")
    try:
        accounts.set_password(u["id"], body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _sign_in(request, response, u)
    return {"ok": True}


# -------------------------------------------------------------- api keys ----
class KeyBody(BaseModel):
    name: str = ""


@app.get("/api/keys")
def list_keys(request: Request):
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "Sign in first.")
    return {"keys": accounts.api_keys(u["id"]), "base_url": PUBLIC_URL + "/v1"}


@app.post("/api/keys")
def create_key(body: KeyBody, request: Request):
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "Sign in first.")
    try:
        return accounts.new_api_key(u["id"], body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.delete("/api/keys/{kid}")
def revoke_key(kid: int, request: Request):
    u = current_user(request)
    if u is None:
        raise HTTPException(401, "Sign in first.")
    if not accounts.revoke_api_key(u["id"], kid):
        raise HTTPException(404, "No such key.")
    return {"ok": True}


# ------------------------------------------------------------------ chats ----
@app.get("/api/models")
def models():
    return {"models": [{"id": k, "name": v["name"], "description": v["description"]} for k, v in MODELS.items()],
            "default": DEFAULT_MODEL}


@app.get("/api/chats")
def list_chats(request: Request, response: Response):
    return {"chats": accounts.chats(owner_of(request, response))}


@app.get("/api/chats/{cid}")
def get_chat(cid: str, request: Request, response: Response):
    c = accounts.chat(owner_of(request, response), cid)
    if c is None:
        raise HTTPException(404, "No such chat.")
    return {"chat": c, "messages": accounts.messages(cid)}


class RenameBody(BaseModel):
    title: str


@app.patch("/api/chats/{cid}")
def rename_chat(cid: str, body: RenameBody, request: Request, response: Response):
    if not accounts.rename_chat(owner_of(request, response), cid, body.title):
        raise HTTPException(404, "No such chat.")
    return {"ok": True}


@app.delete("/api/chats/{cid}")
def delete_chat(cid: str, request: Request, response: Response):
    if not accounts.delete_chat(owner_of(request, response), cid):
        raise HTTPException(404, "No such chat.")
    for b in list(_brains.values()):
        b.reset_conversation("c." + cid)
    return {"ok": True}


class ChatBody(BaseModel):
    message: str
    model: str = DEFAULT_MODEL
    chat_id: Optional[str] = None


def _rehydrate(brain: Brain, session: str, cid: str) -> None:
    """After a restart the brain's conversation context is empty: refill it from the saved chat."""
    ctx = (brain.of_kind("context") or [None])[0]
    if ctx is None or ctx.session(session):
        return
    for m in accounts.messages(cid, limit=8):
        ctx.append_chat("user" if m["role"] == "user" else "brain", m["text"], session)


@app.post("/api/chat")
def chat(body: ChatBody, request: Request, response: Response):
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "empty message")
    if len(text) > 4000:
        raise HTTPException(413, "message too long (4000 chars)")
    if body.model not in MODELS:
        raise HTTPException(400, f"unknown model {body.model!r}")
    if not _chat_rl.allow(client_ip(request)):
        raise HTTPException(429, "Slow down a little.")
    owner = owner_of(request, response)
    c = accounts.chat(owner, body.chat_id) if body.chat_id else None
    if c is None:
        title = text.splitlines()[0][:48] + ("…" if len(text) > 48 else "")
        c = accounts.new_chat(owner, body.model, title)
    session = "c." + c["id"]
    brain = get_brain(body.model)
    _rehydrate(brain, session, c["id"])
    api = brain.endpoint("api")
    t0 = time.time()
    try:
        if api is not None:
            answer = api.think(text, session=session, user=owner)["answer"]   # the api endpoint node originates the thought
        else:
            answer = brain.think(text, session=session, user=owner).answer
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"the brain could not complete that thought: {type(e).__name__}: {e}")
    answer = answer or "(no answer)"
    accounts.add_messages(c["id"], [("user", text, t0), ("assistant", answer, time.time())])
    _log.info("chat %s model=%s %.2fs", owner[:10], body.model, time.time() - t0)
    return {"model": body.model, "answer": answer, "chat_id": c["id"], "title": c["title"]}


# The api and training endpoint nodes listen on localhost; on the Space this app is the only open port,
# so it forwards /v1/* and /train to them (the Cloudflare Worker moves the caller's key to X-Arety-Authorization,
# because Authorization carries the Space's own access token).
def _forward(port_env: str, port_default: str):
    async def handler(request: Request, path: str = ""):
        import httpx

        headers = {"Content-Type": request.headers.get("content-type", "application/json")}
        auth = request.headers.get("x-arety-authorization") or request.headers.get("authorization")
        if auth:
            headers["Authorization"] = auth
        url = f"http://127.0.0.1:{os.environ.get(port_env, port_default)}{request.url.path}"
        if request.url.query:
            url += "?" + request.url.query
        async with httpx.AsyncClient(timeout=900) as c:
            r = await c.request(request.method, url, content=await request.body(), headers=headers)
        return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
    return handler


app.add_api_route("/v1/{path:path}", _forward("ARETY_API_PORT", "8003"), methods=["GET", "POST"], include_in_schema=False)
app.add_api_route("/train", _forward("ARETY_TRAIN_PORT", "8004"), methods=["GET", "POST"], include_in_schema=False)
app.add_api_route("/train/{path:path}", _forward("ARETY_TRAIN_PORT", "8004"), methods=["GET", "POST"], include_in_schema=False)


@app.get("/api/health")
def health():
    return {"ok": True}


@app.on_event("startup")
def _startup():
    start_endpoints()
    accounts.forget_old_guests()


# ----------------------------------------------------------------- static ----
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


@app.get("/")
def index():
    return FileResponse(str(HERE / "static" / "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8002")), log_level="info")
