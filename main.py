"""
Superhouse Chatbot - FastAPI server for the floating chat widget.

    GET  /              -> templates/index.html (widget)
    POST /api/chat      -> {message, history[]} -> answer (+ table data)
    GET  /api/overview  -> small KPI snapshot
    GET  /health        -> liveness probe for Render

The server is stateless: the browser keeps the conversation and sends the
last few turns with every request, so it works with any number of workers.
"""
import html
import logging
import os
import re
from typing import List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import chatbot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("superhouse.api")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(BASE_DIR, "templates", "index.html")
TABLE_ROW_LIMIT = 200          # rows sent to the widget per answer

app = FastAPI(title="Superhouse Chatbot", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "assets")), name="static")


class Turn(BaseModel):
    role: str
    content: str = ""
    sql: Optional[str] = None


class ChatRequest(BaseModel):
    message: str = Field(default="", max_length=2000)
    history: List[Turn] = Field(default_factory=list)
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


@app.get("/", response_class=HTMLResponse)
def index():
    with open(TEMPLATE, encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
def health():
    return {"status": "ok", "llm_ready": chatbot.llm_ready()}


@app.get("/api/overview")
def overview():
    return chatbot.db_overview()


@app.post("/api/chat")
def chat(req: ChatRequest):
    # Sync endpoint -> FastAPI runs it in the threadpool, so slow LLM calls don't block the loop.
    history = [t.model_dump() for t in req.history[-chatbot.HISTORY_TURNS:]]
    res = chatbot.ask(req.message, history, gen_ui=req.gen_ui)
    rows = res.get("rows") or []
    cols = res.get("columns") or []
    show_table = bool(rows) and bool(cols)        # the data always follows the 1-2 sentence opening
    return {
        "type": res["type"],
        "message": res["message"],                 # plain markdown (kept for history)
        "html": md_to_html(res["message"]),        # escaped + formatted for the bubble
        "title": res.get("title"),
        "sql": res.get("sql"),
        "attempts": res.get("attempts", 0),
        "columns": cols if show_table else [],
        "rows": [[_cell(v) for v in r] for r in rows[:TABLE_ROW_LIMIT]] if show_table else [],
        "row_count": len(rows),
        "truncated": bool(res.get("truncated")) or len(rows) > TABLE_ROW_LIMIT,
        "chart": res.get("chart") if show_table else None,
    }


@app.exception_handler(Exception)
async def last_line_of_defence(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    msg = "Something went wrong on my side. Please try again in a moment."
    return JSONResponse(status_code=200, content={
        "type": "error", "message": msg, "html": html.escape(msg), "columns": [], "rows": [],
        "row_count": 0, "truncated": False, "sql": None, "title": None, "attempts": 0, "chart": None})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
