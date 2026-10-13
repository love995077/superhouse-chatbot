# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An NL2SQL chat widget for Superhouse Limited (Shoe Division). Users ask questions in English or Hinglish. An OpenAI model (`OPENAI_MODEL`, default `gpt-6-luna`) turns each question into SQLite SQL over `superhouse.db`, and that database is built from the ERP export `Chatbot data.xlsx`. The stack is FastAPI with a single vanilla HTML/CSS/JS page (`templates/index.html`). There is no frontend build step. README.md has the full design and security write-up.

## Commands

```bash
pip install -r requirements-dev.txt        # runtime deps + httpx (for TestClient)
cp .env.example .env                       # set OPENAI_API_KEY
python setup_db.py                         # build superhouse.db from the Excel file and print the reconciliation
                                           # (optional args: <xlsx> <db>)
COOKIE_SECURE=0 uvicorn main:app --reload  # http://localhost:8000; COOKIE_SECURE=0 because the cookie is Secure by default
python chatbot.py "which leather items need to be ordered?"   # run the engine once from the CLI and print the SQL and rows

python test_bot.py            # every check, including live LLM queries (needs a real OPENAI_API_KEY)
python test_bot.py --offline  # no model calls
```

There is no pytest and no linter config. `test_bot.py` is a custom runner with grouped suites: `offline_tests`, `db_lockdown_tests`, `injection_tests`, `api_security_tests` and `live_tests`. To run one suite:

```bash
python -c "import test_bot as t; t.chatbot.ensure_db(); t.injection_tests()"
```

Importing `test_bot` sets `COOKIE_SECURE=0` and small rate limits before it imports `main`, so do the same for any new HTTP tests. Live tests check the bot's rows against hand-written ground-truth SQL (`truth()`), so a query that runs but returns the wrong data fails.

## Architecture

**Data pipeline:** `schema_docs.py` is the single source of truth. It holds the sheet→table map, the header anchors, the Excel header→column maps, the date, text-ID and zero-fill column lists, and the business description of each column. `setup_db.py` uses it to build the DB:
- It finds the real header row with the anchor cell, because the workbook has banner rows above the header.
- It drops the group/total rows, so `SUM()` matches the Excel totals.
- It stores dates as ISO strings and derives `article_no` and `buyer`.
- It creates the `material_stock` view (leather + sole) and a `_meta` table.
- It writes to `*.tmp` and swaps the file in atomically.

`superhouse.db` is gitignored and always generated. To change data, replace the Excel file.

**Request flow:** `main.py` handles `POST /api/chat`, then calls `chatbot.ask(question, history, gen_ui)`, then sends a public payload back to the widget.
- The session history is the earlier questions plus their SQL. It lives server-side in `security.SessionStore`, keyed by the `sh_sid` HttpOnly cookie. The client sends only `{message, gen_ui}`, and any extra fields such as a forged `history` are ignored.
- Responses never include SQL or internal table and column names. `main._payload` controls which fields go out, and `test_bot.PUBLIC_KEYS` asserts that set.
- Sessions and rate limits live in the process, so the app **must run as a single uvicorn worker**.

**`chatbot.ask()` pipeline (it never raises):**
1. `firewall()` handles greetings, off-topic questions and jailbreaks with canned replies, before any LLM call.
2. `generate_sql()` sends the question to the LLM. The system prompt is built from **live DB introspection** (`schema_catalog()`: `PRAGMA table_info`, real DISTINCT values and date ranges) joined with `schema_docs`, plus `RULES` (and `GEN_UI_RULES` when Gen UI is on). The model returns JSON with `type` (`sql`, chat or `out_of_scope`), `sql`, `title` and an optional `chart`.
3. `clean_sql()` and `is_safe_sql()` run the text guard, then `run_sql()` executes the query. The connection is read-only with `query_only` and a **SQLite authorizer allowlist** (`ALLOWED_TABLES`, `DENIED_FUNCTIONS`), an 8 s time budget and a row cap.
4. Self-healing: on a SQL error, the error goes back to the LLM for up to `MAX_REPAIRS=3` more attempts. If a valid query returns zero rows, one "soft heal" retry runs.
5. `write_answer()` produces a 1–2 sentence opening, and `opening_only()` enforces that length in code. The table is rendered separately, so the prose must not repeat the data.
6. A leak filter (`leaks_internals()` / `safe_title()` / `_non_sql_message()`) replaces any model text that contains SQL-shaped text or snake_case identifiers from the live DB.
7. With Gen UI on, `chart_spec()` checks the model's chart hint against the real result columns before the widget draws KPIs and a chart.

**Consequences for changes:**
- A new table or view must be added to `ALLOWED_TABLES` in `chatbot.py`, or the authorizer blocks it. It also needs mappings and docs in `schema_docs.py`.
- New snake_case column names automatically become "internal", and the leak filter hides them. The widget's column labels come from `main.display_labels()`.
- `test_bot.py` duplicates some user-facing strings, such as `GREETING`. Update them together with `chatbot.GREETING_REPLY` and the related messages.
- CSP uses a per-request nonce injected into `index.html` by `main.index()`. An inline `<script>`/`<style>` without the nonce is blocked, and so is an external resource.

## Deployment

- **Production (current):** Docker on an Oracle VM behind a Caddy layer-4 proxy: `docker compose up -d --build`. The container listens on `127.0.0.1:8001`, runs non-root with a read-only root FS, and rebuilds the DB from the bundled Excel on every start into the bind-mounted `./data`. uvicorn runs with `--workers 1 --no-proxy-headers`. Because of the proxy, the app never sees real client IPs, so rate limits are keyed per session plus a global cap, never per IP.
- **Render (legacy):** `python deploy_render.py` creates or redeploys the service through the Render API. It needs `RENDER_API_KEY` in `.env`.

Environment knobs: `OPENAI_API_KEY`, `OPENAI_MODEL`, `SUPERHOUSE_DB`, `COOKIE_SECURE`, `RATE_LIMIT_PER_SESSION`, `RATE_LIMIT_GLOBAL`, `MAX_CONCURRENT_QUESTIONS`.
