"""
Superhouse Chatbot - FastAPI server for the full-screen chat page.

    GET  /              -> templates/index.html (chat page, served with a per-request CSP nonce)
    POST /api/chat      -> {message, gen_ui} -> bot message + table data (never SQL)
    POST /api/reset     -> forget this browser's conversation context
    GET  /health        -> liveness probe

Conversation context (earlier questions and the SQL behind them) lives server-side in a
SessionStore keyed by an opaque HttpOnly cookie, so the browser never sees or supplies SQL.
State is in-process: run ONE uvicorn worker.
"""
import html
import logging
import os
import re
import secrets
import threading
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

import chatbot
from security import BodySizeLimitMiddleware, SecurityHeadersMiddleware, SessionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("superhouse.api")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(BASE_DIR, "templates", "index.html")
TABLE_ROW_LIMIT = 200                                   # rows sent to the page per answer
COOKIE_NAME = "sh_sid"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "1") != "0"   # HTTPS in production; set 0 only for plain-http tests
SESSION_TTL = 2 * 3600

sessions = SessionStore(
    ttl_seconds=SESSION_TTL,
    max_turns=chatbot.HISTORY_TURNS * 2,
    per_session_limit=int(os.getenv("RATE_LIMIT_PER_SESSION", "20")),    # requests / minute / session
    global_limit=int(os.getenv("RATE_LIMIT_GLOBAL", "120")),             # requests / minute / server
)
llm_slots = threading.BoundedSemaphore(int(os.getenv("MAX_CONCURRENT_QUESTIONS", "8")))

# openapi_url=None: no public /openapi.json, /docs or /redoc describing the API.
app = FastAPI(title="Superhouse Chatbot", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(BodySizeLimitMiddleware, max_bytes=16 * 1024)
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="static")


class ChatRequest(BaseModel):
    # Unknown fields (e.g. a forged "history") are ignored - context only ever comes from the server-side session.
    model_config = ConfigDict(extra="ignore")
    message: str = Field(default="", max_length=2000)
    gen_ui: bool = False          # "📊 Gen UI" toggle: ask for a chart/KPI dashboard with the table


def md_to_html(text: str) -> str:
    """Tiny, safe markdown subset (escape first): bold, italic, code, bullet lists, line breaks."""
    out, in_list = [], False
    for line in html.escape(text or "").splitlines():
        s = line.strip()
        m = re.match(r"^([-*•]|\d+[.)])\s+(.*)$", s)
        if m:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{m.group(2)}</li>")
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        out.append(s + "<br>" if s else "<br>")
    if in_list:
        out.append("</ul>")
    h = "".join(out)
    h = re.sub(r"(<br>){3,}", "<br><br>", h)
    h = re.sub(r"^(<br>)+|(<br>)+$", "", h)
    h = re.sub(r"<br>(<ul>)", r"\1", h)
    h = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", h)
    h = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", h)
    h = re.sub(r"`([^`]+)`", r"<code>\1</code>", h)
    return h


def _cell(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, float):
        return round(v, 2)
    return v


def display_labels(cols):
    """Map SQL column names/aliases to human labels so no internal identifier reaches the browser."""
    labels, seen = {}, {}
    for c in cols:
        base = re.sub(r"\s+", " ", str(c).replace("_", " ")).strip().title() or "Value"
        n = seen.get(base, 0) + 1
        seen[base] = n
        labels[c] = base if n == 1 else f"{base} {n}"
    return labels


def _payload(kind, message, title=None, cols=(), rows=(), row_count=0, truncated=False, chart=None):
    """The ONLY shape the client ever receives: bot message + display data. No SQL, no internals."""
    return {"type": kind, "html": md_to_html(message), "title": title, "columns": list(cols),
            "rows": list(rows), "row_count": row_count, "truncated": truncated, "chart": chart}


def _set_session_cookie(response: Response, sid: str):
    response.set_cookie(COOKIE_NAME, sid, max_age=SESSION_TTL, httponly=True, secure=COOKIE_SECURE,
                        samesite="strict", path="/api")


@app.get("/", response_class=HTMLResponse)
def index():
    nonce = secrets.token_urlsafe(18)
    with open(TEMPLATE, encoding="utf-8") as f:
        page = f.read().replace("<script>", f'<script nonce="{nonce}">').replace("<style>", f'<style nonce="{nonce}">')
    csp = ("default-src 'none'; "
           f"script-src 'nonce-{nonce}'; "
           f"style-src 'self' 'nonce-{nonce}' https://fonts.googleapis.com; "
           "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    return HTMLResponse(page, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


@app.get("/health")
def health():
    return {"status": "ok", "llm_ready": chatbot.llm_ready()}


@app.post("/api/chat")
def chat(req: ChatRequest, request: Request, response: Response):
    # Sync endpoint -> runs in the threadpool, so slow LLM calls don't block the event loop.
    sid = sessions.resolve(request.cookies.get(COOKIE_NAME))
    allowed, retry_after = sessions.allow(sid)
    if not allowed:
        r = JSONResponse(status_code=429, headers={"Retry-After": str(retry_after)}, content=_payload(
            "error", "You're sending questions a little too quickly. Please wait a moment and try again."))
        _set_session_cookie(r, sid)
        return r
    if not llm_slots.acquire(blocking=False):
        r = JSONResponse(status_code=503, headers={"Retry-After": "5"}, content=_payload(
            "error", "The assistant is busy right now. Please try again in a few seconds."))
        _set_session_cookie(r, sid)
        return r
    try:
        res = chatbot.ask(req.message, sessions.history(sid), gen_ui=req.gen_ui)
    finally:
        llm_slots.release()

    # Context for follow-ups (incl. the SQL) stays on the server.
    sessions.append(sid, {"role": "user", "content": req.message},
                    {"role": "assistant", "content": res["message"], "sql": res.get("sql")})
    _set_session_cookie(response, sid)

    rows, cols = res.get("rows") or [], res.get("columns") or []
    if not (rows and cols):
        return _payload(res["type"], res["message"], title=res.get("title"))
    labels = display_labels(cols)
    chart = res.get("chart")
    if chart:
        chart = {"type": chart.get("type"), "x": labels.get(chart.get("x")), "y": labels.get(chart.get("y")),
                 "measures": [labels[m] for m in chart.get("measures", []) if m in labels]}
    return _payload(res["type"], res["message"], title=res.get("title"),
                    cols=[labels[c] for c in cols],
                    rows=[[_cell(v) for v in r] for r in rows[:TABLE_ROW_LIMIT]],
                    row_count=len(rows), truncated=bool(res.get("truncated")) or len(rows) > TABLE_ROW_LIMIT,
                    chart=chart)


@app.post("/api/reset")
def reset(request: Request, response: Response):
    """'New conversation': forget the server-side context and rotate the session id."""
    old: Optional[str] = request.cookies.get(COOKIE_NAME)
    if old:
        sessions.drop(old)
    _set_session_cookie(response, sessions.resolve(None))
    return {"ok": True}


# ---------------------------------------------------------------------------
# Error handling - generic messages only; details go to the server log
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, exc: RequestValidationError):
    log.info("rejected invalid request on %s: %s", request.url.path, str(exc.errors())[:300])
    return JSONResponse(status_code=400, content=_payload("error", "That request couldn't be processed."))


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    msg = {404: "Not found.", 405: "Method not allowed."}.get(exc.status_code, "Request failed.")
    return JSONResponse(status_code=exc.status_code, content={"detail": msg})


@app.exception_handler(Exception)
async def last_line_of_defence(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content=_payload(
        "error", "Something went wrong on my side. Please try again in a moment."))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False,
                proxy_headers=False, server_header=False)
