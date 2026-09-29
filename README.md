# Superhouse Chatbot

AI assistant for **Superhouse Limited (Shoe Division)**. Ask questions in English or Hinglish about material stock, shortfalls, sale orders, samples and internal orders, and get answers straight from the ERP export (`Chatbot data.xlsx`).

Built as a **floating chat widget** (FastAPI + vanilla HTML/CSS/JS) on top of an OpenAI-powered NL2SQL engine over SQLite, following the *NL2SQL Architect* methodology:

| Pillar | Implementation |
|---|---|
| Schema & ground truth | The LLM prompt is built from **live introspection** of `superhouse.db` (`PRAGMA table_info` plus verified `DISTINCT` values and date ranges), joined with business descriptions in `schema_docs.py`. |
| Dashboard reconciliation | `setup_db.py` drops the Excel group/total rows and prints a reconciliation, so `SUM()` in SQL equals the Excel totals exactly. MRP formulas (`short_fall`, `excess_qty`, `to_be_ord`) were verified against every row. |
| Zero-crash | `chatbot.ask()` never raises. Zero rows or all-NULL aggregates return an explicit "no records" message. A read-only SQLite connection, an SQL guard and a query time budget keep every generated query safe. |
| Conversational resilience | The prompt covers relative date maths (IST), fuzzy `LIKE` matching, a glossary for implicit statuses, cross-sheet join keys (`order_no`, `article_no`) and chat-history follow-ups. |
| Self-healing | Up to **3 repair attempts**, each feeding the SQLite error back to the LLM, plus one soft-heal pass when a valid query returns nothing. |

## Data model (`superhouse.db`)

| Table | Source sheet | Rows |
|---|---|---|
| `leather_stock` | Leather (MRP) | 133 items |
| `sole_stock` | Sole (MRP) | 837 items |
| `material_stock` | *view*: leather + sole, with `category` | 970 |
| `sale_orders` | Sale order | 369 lines |
| `samples` | sample | 1,419 |
| `io_to_last` | io to last | 33 IOs |

The real Excel header sits at **row index 2** (row 0 is the company banner and row 1 is the report title), so it is auto-detected per sheet. Dates are stored as ISO `YYYY-MM-DD`. The derived columns `article_no` (all order sheets) and `buyer` (sale orders) make cross-sheet questions possible.

## Architecture

| File | Role |
|---|---|
| `main.py` | FastAPI app. `GET /` serves the widget (with a per-request CSP nonce), `POST /api/chat` answers questions, `POST /api/reset` starts a new conversation, `GET /health` is the liveness probe |
| `security.py` | Server-side session store, rate limiting, security headers and the request body-size limit |
| `templates/index.html` | Floating chat widget in a slate corporate style. It has a `#0F172A` header with an Online dot; white bot bubbles and `#334155` user bubbles; **borderless zebra-striped HTML tables** inside bot bubbles (no CSV/SQL controls are exposed to end users); quick-action chips; and a pill input bar with the **📊 Gen UI** toggle |
| `chatbot.py` | NL2SQL engine: firewall, live-schema prompt, read-only guard, self-healing loop and answer writing |
| `schema_docs.py` | Excel-to-SQLite column maps and business descriptions |
| `setup_db.py` | Builds `superhouse.db` from the Excel file and reconciles totals |
| `deploy_render.py` | Creates or updates the Render web service through the Render API |
| `test_bot.py` | Automated tests: offline pillar checks, plus live AI queries verified row-by-row against ground-truth SQL |

**Answer style:** every data answer is a 1-2 sentence professional opening followed immediately by the result table. There are no bullet summaries that repeat the data; this is enforced both in the prompt and in code (`opening_only`).

**📊 Gen UI mode:** when the toggle is on, the model shapes the query to be chartable and returns a chart hint. The server validates that hint against the real result columns, and the widget renders:
- a KPI row (record count, total or average, top item)
- a bar chart for rankings or a line chart for date trends, with hover tooltips and a measure switcher
- the full data table

Conversation context lives **server-side**. That includes earlier questions and the SQL behind them, which follow-ups like "what about Dockers?" rely on. It is keyed by an opaque, HttpOnly session cookie. The browser sends only `{message, gen_ui}` and receives only the bot message and display data, so no SQL, internal table names or column names ever reach it. State is in-process, so run a **single** uvicorn worker (the Docker image does).

## Security

| Layer | Protection |
|---|---|
| Database engine | SQLite **authorizer allowlist** for AI-generated SQL: only `SELECT` reads of the 5 business tables are allowed. Everything else is refused: writes, DDL, `ATTACH`, `PRAGMA`, `sqlite_master`, `pragma_*()` functions, dangerous functions. Also a read-only connection plus `query_only`, a 1 MB value cap, a query-length cap, an 8 s time budget and a row cap. |
| SQL text guard | Single `SELECT`/`WITH` statement only, with keyword blocklist (defence in depth) |
| Prompt injection | Security rules in the system prompt; a jailbreak pre-filter; an **output leak filter** that replaces any model text (reply, opening, title) containing SQL or internal identifiers; client-supplied history is ignored |
| API | Only public fields returned; no `/openapi.json`, `/docs` or `/redoc`; generic errors, with details only in server logs; 16 KB body limit |
| Abuse / DoS | Per-session (20/min) and global (120/min) rate limits → 429; at most 8 concurrent questions → 503; uvicorn `--limit-concurrency 64` |
| Browser | CSP with per-request nonce (no inline script execution otherwise), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, HSTS, COOP/CORP, `no-store` on API responses, no CORS; cookie `HttpOnly; SameSite=Strict; Secure; Path=/api` |
| Container | Non-root, read-only root filesystem, all capabilities dropped, `no-new-privileges`, memory/PID limits, rotated logs, loopback-only port, only the needed environment variables passed in |

Behind the layer-4 Caddy proxy every request arrives from 127.0.0.1, so there is no trustworthy client IP. Rate limits are therefore keyed by session plus a global cap, and uvicorn runs with `--no-proxy-headers` so forged `X-Forwarded-For` headers are ignored.

## Run locally

```bash
python -m venv venv
venv\Scripts\activate            # Windows  (source venv/bin/activate on Linux/Mac)
pip install -r requirements.txt
copy .env.example .env           # then put your real OPENAI_API_KEY in .env
python setup_db.py               # builds superhouse.db and prints reconciliation
uvicorn main:app --reload        # open http://localhost:8000
```

Open the app and sign in with the demo credentials `admin` / `admin`. The sign-in screen, role selector and
chat-history sidebar are a frontend-only presentation layer: they don't restrict or store anything on the server.

Brand assets live in `assets/` (served at `/static/`): `superhouse-logo.png` is the official logo (sign-in page) and
`superhouse-mark.png` is the tower mark cut from it (favicon, widget header, bot avatar).

## Tests

```bash
pip install -r requirements-dev.txt
python test_bot.py            # offline, security and API checks + live AI queries (needs OPENAI_API_KEY)
python test_bot.py --offline  # no model calls
```

## Deploy to Render

```bash
# .env must contain RENDER_API_KEY (and ideally a real OPENAI_API_KEY)
python deploy_render.py
```

The script creates the web service if it doesn't exist, or redeploys it if it does. The service runs:
- **Build command:** `pip install -r requirements.txt && python setup_db.py`
- **Start command:** `uvicorn main:app --host 0.0.0.0 --port 10000` (Render's default port)
- **Health check:** `/health`

To refresh the data, replace `Chatbot data.xlsx` and push. Render rebuilds the database automatically.
