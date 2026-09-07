"""Web frontend for chatgpx.

Serves a single static page (static/index.html) with a login view and a chat
view, plus the two HTTP endpoints that glue the page to the rest of the app:

- POST /api/login  -> gpx_sync.download_all_gpx(email, password, GPX_DIR)
- POST /api/chat   -> chat_backend.chat(message)

Both integrations are optional at import time: until the komoot-data and
chat-inference agents land their modules, the corresponding endpoint replies
with 503 instead of crashing the whole app, so the frontend/Docker scaffolding
can be built and run in parallel.

This is a single-user demo: there is no real account system or database.
Sessions are an in-memory dict keyed by an opaque cookie, and the Komoot
password is only ever held in memory for the duration of the login call - it
is never logged or persisted.
"""

import logging
import secrets
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("chatgpx.app")

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
GPX_DIR = BASE_DIR / "activities"
SESSION_COOKIE = "chatgpx_session"

try:
    import gpx_sync
except ImportError:
    gpx_sync = None
    logger.warning("gpx_sync module not found yet - /api/login will return 503")

try:
    import chat_backend
except ImportError:
    chat_backend = None
    logger.warning("chat_backend module not found yet - /api/chat will return 503")

app = FastAPI(title="chatgpx")

# session_id -> {"activity_count": int}. Never holds the Komoot password.
SESSIONS: dict[str, dict] = {}


@app.on_event("startup")
async def _start_chat_backend():
    # Eagerly spins up chat_backend's MCP stdio connection to server.py so the
    # first chat request doesn't pay that startup cost. Best-effort: a broken
    # or not-yet-ready chat_backend shouldn't take the whole app down, since
    # login/GPX sync doesn't depend on it.
    if chat_backend is not None:
        try:
            await chat_backend._default_session.start()
        except Exception:
            logger.exception("chat_backend failed to start; /api/chat will error until it's fixed")


@app.on_event("shutdown")
async def _stop_chat_backend():
    if chat_backend is not None:
        await chat_backend._default_session.stop()


class LoginRequest(BaseModel):
    email: str
    password: str
    tour_type: str = "tour_all"
    start_date: str | None = None
    end_date: str | None = None
    sport: str | None = None


class ChatRequest(BaseModel):
    message: str


def _get_session(request: Request) -> dict | None:
    session_id = request.cookies.get(SESSION_COOKIE)
    return SESSIONS.get(session_id) if session_id else None


def _require_session(request: Request) -> dict:
    session = _get_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Not logged in")
    return session


@app.post("/api/login")
async def login(payload: LoginRequest, response: Response):
    if gpx_sync is None:
        raise HTTPException(status_code=503, detail="GPX sync is not available yet")

    try:
        files = await run_in_threadpool(
            gpx_sync.download_all_gpx,
            payload.email,
            payload.password,
            GPX_DIR,
            payload.tour_type,
            "en",
            payload.start_date,
            payload.end_date,
            payload.sport,
        )
    except ValueError as exc:
        # Bad date format, etc. - a 400, not a 401 (nothing wrong with the
        # credentials).
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        # gpx_sync is responsible for not leaking credentials in its own
        # exception messages/logs; we just avoid ever logging the payload here.
        logger.info("Login failed: %s", exc)
        raise HTTPException(status_code=401, detail=f"Login failed: {exc}") from exc

    session_id = secrets.token_urlsafe(32)
    SESSIONS[session_id] = {"activity_count": len(files)}
    response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="lax")
    return {"activities": len(files)}


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request):
    _require_session(request)
    if chat_backend is None:
        raise HTTPException(status_code=503, detail="Chat backend is not available yet")

    try:
        reply = await chat_backend.chat(payload.message)
    except Exception as exc:
        logger.exception("chat_backend.chat() failed")
        raise HTTPException(status_code=502, detail=f"Chat backend error: {exc}") from exc
    return {"reply": reply}


@app.get("/api/status")
async def status(request: Request):
    session = _get_session(request)
    return {
        "logged_in": session is not None,
        "activities": session["activity_count"] if session else 0,
    }


@app.post("/api/logout")
async def logout(request: Request, response: Response):
    session_id = request.cookies.get(SESSION_COOKIE)
    SESSIONS.pop(session_id, None)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


# Mounted last so the /api/* routes above take precedence over this catch-all.
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
