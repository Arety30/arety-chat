# Arety Chat

Arety Chat is a small, text-only web chat application written by Syed Abrar Ali. It puts a browser UI, user accounts and a `/v1`-style HTTP API in front of the RVTCB "brain" (the `rvtcb` Python package, kept in a separate private repository), so people can talk to the brain from a web page or from their own programs without ever seeing its internals. The server exposes only model names and answers; everything about how an answer is produced stays server-side. It solves the problem of hosting a self-built reasoning engine as a normal, multi-user chat service: sign-up and login, saved conversations, password reset by email, per-user API keys, rate limiting, and a way to persist all of that when the host is a throwaway container.

## Where it is used

Arety Chat is the public face of the RVTCB project. It runs as a container on a hosted "Space" style platform (see `space/`) behind a reverse proxy, and serves two brain configurations, "Arety RCB 1.0" and "Arety RCB 1.2", to anyone with a browser. Signed-in users can also mint personal API keys and call the brain from scripts through the `/v1/*` route. Because the container has no durable disk, the accounts database, chat history and per-user memory are periodically backed up to a private dataset repository and restored on the next start.

## How it works

### Request flow

1. The browser loads `static/index.html` (a single-page UI with sidebar, model picker, login/signup/forgot/reset views and an API-keys panel) and talks to the FastAPI server over `/api/*`.
2. `POST /api/chat` validates the message (non-empty, at most 4000 characters, known model id), applies a per-IP limiter (60 requests/minute), and resolves the *owner* of the request: `u<id>` for a signed-in account or `g.<session-id>` for a guest cookie.
3. The chat is looked up or created in SQLite (title = first line of the first message, trimmed to 48 characters). The conversation key passed to the brain is `c.<chat_id>`.
4. One `Brain` instance per model id is created lazily and cached. Each is built with its own layout (`LEGACY_LAYOUT` for 1.0, `SERVING_LAYOUT` for 1.2, the latter with consensus enabled), a per-model data directory, and optional knowledge resources (knowledge directory, dictionary, knowledge DB, generator weights, encyclopedia DB) taken from environment variables.
5. After a restart the brain's in-memory context is empty, so `_rehydrate()` replays the last 8 saved messages of the chat into the brain's `context` node before thinking.
6. The thought is originated through the brain's `api` endpoint node when present (`api.think(text, session, user)`), otherwise `brain.think(...)`. The answer and the user turn are appended to the `messages` table and returned as JSON.

### Accounts and security (`accounts.py`)

- One SQLite file (`data/accounts.sqlite`, mode 600, WAL) with tables `users`, `sessions`, `resets`, `chats`, `messages`, `api_keys`.
- Passwords are scrypt-hashed (n=16384, r=8, p=1, 16-byte salt). Session tokens, reset tokens and API keys are stored only as SHA-256 hashes; a stolen database does not grant access. Login does a dummy hash check for unknown emails so timing is equal.
- Sessions last 30 days (`ac_auth` cookie, HttpOnly, Secure, SameSite=Lax). Guests get a 32-hex `ac_sid` cookie; signing in adopts the guest's chats into the account and calls `brain.merge_user()` so what the brain learned about the guest follows them.
- Password reset: a single-use token valid for 60 minutes, mailed through a local SMTP bridge; the response is identical whether or not the email exists. Changing a password invalidates all sessions.
- Rate limits: login 10/15 min per IP+email, sign-up 5/hour per IP, forgot 3/hour per email and 10/hour per IP.
- A middleware refuses cookie-authenticated writes whose `Origin` is not the configured public URL (CSRF protection).
- API keys: up to 10 active per user, format `ak_` + random; `resolve_api_key()` maps a key back to `u<id>` so API calls share that account's memory. Guests' chats older than 14 days are purged at startup.

### Endpoint nodes and the public API

At startup the default brain's two special nodes, `api` and `training`, are served on loopback ports (8003 and 8004 by default) with key lists from `ARETY_API_KEYS` / `ARETY_TRAIN_KEYS`, and the `api` node is given the account key resolver. Because the container exposes a single port, the FastAPI app forwards `/v1/*` and `/train*` to those loopback servers with `httpx`, copying the caller's key from `X-Arety-Authorization` (or `Authorization`) into the upstream `Authorization` header.

### Container lifecycle (`space/`)

- `Dockerfile`: Python 3.12 slim image with FastAPI, uvicorn, pydantic, httpx, `huggingface_hub` and numpy; runs `start.sh`.
- `fetch.py`: downloads the brain code and its knowledge folder from a private model repository, copies teacher-generated information files alongside, downloads the dictionary, encyclopedia and knowledge SQLite databases, then restores the saved site data (accounts, chats, memory) from a private dataset repository. If only 1.0's memory exists, it is copied as the starting memory for 1.2.
- `persist.py`: every 10 minutes snapshots the data directory (SQLite files via the online backup API, everything else copied) and uploads it if anything changed; squashes history after 100 commits; also saves on SIGTERM.

### Knowledge files (`knowledge/`)

JSON fact files grouped by brain node and sub-node: `information/*` (geography, science, technology, history, people, mathematics, everyday), `language/*` (lexicon, relations) and `processing/rules.json` (inference rules such as "X is a mammal implies X is an animal", unit conversions, comparisons). Each file lists its topics, a count and the facts as subject/relation/object triples; `manifest.json` indexes them.

### Component table

| Path | Role |
| --- | --- |
| `server.py` | FastAPI app: models, chats, auth, API keys, `/v1` and `/train` forwarding, static UI |
| `accounts.py` | SQLite account store, rate limiter, password hashing, reset mail sender |
| `static/index.html` | Single-page browser UI |
| `knowledge/` | Teacher-generated fact triples loaded by the brain |
| `space/` | Container build, fetch and persist scripts for the hosted deployment |
| `tools/noreply_guard.py` | Cron-style script that moves anything mailed to the no-reply address to Trash |

## Setup

Requirements: Python 3.12+, the private `rvtcb` package importable on `PYTHONPATH`, and `pip install fastapi uvicorn pydantic httpx numpy`. For password-reset mail you also need a local IMAP/SMTP bridge and an accounts file in the format used by the companion mail MCP project.

Environment variables (all optional unless stated):

| Variable | Meaning | Example |
| --- | --- | --- |
| `HOST`, `PORT` | Bind address of the web server | `0.0.0.0`, `8002` |
| `ARETY_PUBLIC_URL` | Public origin, used for CSRF checks and reset links | `https://example.com` |
| `ARETY_CHAT_DATA` | Data directory (accounts DB, per-model brain data) | `~/arety-chat/data` |
| `ARETY_KNOWLEDGE_DIR` | Folder of knowledge JSON files | `~/knowledge` |
| `ARETY_DICTIONARY`, `ARETY_KNOWLEDGE_DB`, `ARETY_ENCYCLOPEDIA`, `ARETY_GENERATOR` | Paths to the brain's optional resources | `/path/to/x.sqlite` |
| `ARETY_API_KEYS`, `ARETY_TRAIN_KEYS` | Comma-separated static keys for the api/training endpoint nodes | `key1,key2` |
| `ARETY_API_PORT`, `ARETY_TRAIN_PORT` | Loopback ports of the two endpoint nodes | `8003`, `8004` |
| `ARETY_MAIL_ACCOUNTS`, `ARETY_MAIL_FROM`, `ARETY_SMTP_HOST`, `ARETY_SMTP_PORT` | Where reset mail is sent from | `~/.config/mail/accounts.json`, `no-reply@example.com`, `127.0.0.1`, `1026` |
| `HF_TOKEN` (container only) | Token used by `space/fetch.py` and `space/persist.py` | placeholder |

## Usage

Run locally:

```bash
cd ~/arety-chat
PYTHONPATH=/path/to/rvtcb ARETY_PUBLIC_URL=http://127.0.0.1:8002 python server.py
# open http://127.0.0.1:8002
```

Talk to the brain from the command line:

```bash
curl -s -c jar -b jar http://127.0.0.1:8002/api/models
curl -s -c jar -b jar -X POST http://127.0.0.1:8002/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"What is the capital of France?","model":"arety-rcb-1.2"}'
# -> {"model":"arety-rcb-1.2","answer":"...","chat_id":"...","title":"What is the capital of France?"}
```

Account flow: `POST /api/signup` and `/api/login` with `{"email","password"}` set the auth cookie; `GET /api/me` shows who you are; `GET/POST/DELETE /api/keys` manage API keys; `GET /api/chats`, `GET/PATCH/DELETE /api/chats/{id}` manage saved chats; `POST /api/forgot` and `/api/reset-password` handle resets; `GET /api/health` is the liveness check.

Deploy as a container: build `space/Dockerfile` with the repo contents, set `HF_TOKEN`, and run; `start.sh` fetches resources, exports the `ARETY_*` paths and starts the server plus the persistence loop on port 7860.

## Project structure

```
arety-chat/
├── server.py              # FastAPI application and brain wiring
├── accounts.py            # SQLite accounts, chats, API keys, rate limiter, mail
├── static/
│   └── index.html         # browser UI
├── knowledge/
│   ├── manifest.json
│   ├── information/*.json
│   ├── language/*.json
│   └── processing/rules.json
├── space/
│   ├── Dockerfile
│   ├── start.sh
│   ├── fetch.py           # pull brain code, databases and saved site data
│   └── persist.py         # periodic backup of accounts/chats/memory
├── tools/
│   └── noreply_guard.py   # trash replies sent to the no-reply mailbox
└── data/                  # runtime data (not published)
```

## Limitations and notes

- The `rvtcb` package that implements the brain is not part of this repository; without it the server cannot start.
- Text only: no attachments, images, voice or streaming responses. Messages are capped at 4000 characters.
- Only the last 8 saved messages are replayed into the brain after a restart, so very long conversations lose older in-brain context.
- The API docs routes (`/docs`, `/redoc`, `/openapi.json`) are disabled on purpose.
- Cookies are set with the `Secure` flag, so the UI expects to be served over HTTPS (or through a TLS-terminating proxy) in production; the same-origin check also allows the local `http://127.0.0.1:PORT` origin for development.
- `space/fetch.py` and `space/persist.py` reference private repositories by name and need an access token; they are specific to the hosted deployment and are not required for local use.
- Guest chats are deleted after 14 days of inactivity; signed-in users' chats are kept until they delete them.
