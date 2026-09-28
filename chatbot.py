"""
Superhouse NL2SQL engine.

Pipeline (every stage degrades to a polite message - `ask()` never raises):
    1. Off-topic / chit-chat firewall            (no SQL for "tell me a joke")
    2. LLM -> JSON {type: sql|chat|clarify}     (schema = live DB introspection)
    3. Read-only guard                           (SELECT/WITH only, 1 statement)
    4. Execute on a read-only SQLite connection with a time budget
    5. Self-healing loop: up to 3 repairs, feeding the SQLite error back
    6. Zero rows / all-NULL aggregate -> explicit "no records" answer
       (after one soft-heal pass that re-checks literals and filters)
    7. LLM summarises the actual rows (never invents numbers)
"""
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from schema_docs import TABLE_DOCS

load_dotenv()
log = logging.getLogger("superhouse.chatbot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("SUPERHOUSE_DB", os.path.join(BASE_DIR, "superhouse.db"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-6-luna")
MAX_REPAIRS = 3
MAX_ROWS = 500              # rows returned to the UI
SUMMARY_ROWS = 40           # rows shown to the LLM for the written answer
QUERY_TIME_BUDGET_S = 8.0
HISTORY_TURNS = 6
IST = timezone(timedelta(hours=5, minutes=30))

NO_RECORD_MSG = "No records match this criteria in the Superhouse data. Try widening the filter (e.g. a shorter name or a larger date range). 🙏"
GAVE_UP_MSG = ("I couldn't work this one out even after 3 attempts. Please rephrase it a little more "
               "specifically - e.g. *'leather items to be ordered'*, *'open samples of Tanmay'* or "
               "*'sale orders shipping this month'*.")
GENERIC_FAIL_MSG = "I'm having trouble answering that right now. Please try again in a moment."
NO_KEY_MSG = ("The AI engine isn't configured yet - set a valid `OPENAI_API_KEY` in the environment "
              "(.env locally, or the Render dashboard) and reload.")

# ---------------------------------------------------------------------------
# Firewall
# ---------------------------------------------------------------------------
GREETING = re.compile(
    r"^\s*(hi+|hello+|hey+|hii+|namaste|namaskar|good\s*(morning|afternoon|evening|night)|"
    r"thanks?|thank\s*you|thx|ok(ay)?|cool|great|nice|bye|shukriya|dhanyavaad|kaise\s+ho|how\s+are\s+you)"
    r"[\s!.?🙏😊]*$", re.I)
DOMAIN_WORDS = {
    "stock", "leather", "sole", "material", "item", "order", "orders", "sale", "sales", "sample", "samples",
    "buyer", "merchandiser", "shortfall", "short", "excess", "shipment", "ship", "destination", "io",
    "internal", "last", "lasting", "cutting", "cut", "pending", "balance", "qty", "quantity", "pair", "pairs",
    "article", "style", "colour", "color", "lining", "csa", "factory", "ex-factory", "po", "purchase",
    "requirement", "required", "issue", "issued", "mrp", "walmart", "dockers", "zign", "next", "m&s",
    "open", "completed", "cancelled", "development", "confirmation", "pps", "euro", "gbp", "usd", "inr",
    "overdue", "delayed", "late", "due", "value", "amount", "total", "count", "how many", "kitna", "kitne",
    "kaunse", "konse", "list", "show", "batao", "dikhao", "report", "conveyor", "neolite", "swede", "cow",
    "buff", "split", "tpr", "eva", "group", "uom", "shoe", "sandle", "chappal", "tanmay", "fazil", "rajeev",
    "musheer", "waqar", "chandan", "kaif", "tajwar", "sea", "air", "road",
}
OFF_TOPIC_WORDS = {
    "weather", "joke", "jokes", "cricket", "football", "movie", "movies", "song", "songs", "politics",
    "election", "recipe", "news", "bitcoin", "crypto", "horoscope", "poem", "girlfriend", "boyfriend",
    "ipl", "netflix", "game", "games", "capital of", "prime minister", "president", "write code", "python code",
}
GREETING_REPLY = ("Hello! 👋 I'm the Superhouse data assistant. Ask me about **leather & sole stock**, "
                  "**sale orders**, **samples** or **internal orders (IO)** - e.g. *'which leather items are "
                  "short?'*, *'Walmart orders shipping this month'* or *'open samples of Tanmay'*.")
OFF_TOPIC_REPLY = ("I can only help with Superhouse business data - material stock & shortfalls, sale orders, "
                   "samples and internal orders. Try *'top 10 sole items to be ordered'* or "
                   "*'orders by destination'*.")


def _words(text: str) -> set:
    return set(re.findall(r"[a-z&\-]+", text.lower()))


def firewall(question: str, has_history: bool):
    """Return a canned reply for greetings / off-topic questions, else None."""
    q = question.strip()
    if GREETING.match(q):
        if re.match(r"\s*(thanks?|thank\s*you|thx|shukriya|dhanyavaad|ok(ay)?|cool|great|nice|bye)\b", q, re.I):
            return "You're welcome! 😊 Ask me anything else about stock, orders, samples or production."
        return GREETING_REPLY
    if has_history and len(q.split()) <= 4:
        return None                      # short follow-up: on-topic by context
    low = q.lower()
    words = _words(low)
    domain = len(words & DOMAIN_WORDS) + sum(1 for w in DOMAIN_WORDS if " " in w and w in low)
    domain += 1 if re.search(r"\b\d{4,5}\b", low) else 0         # order / article numbers
    off = len(words & OFF_TOPIC_WORDS) + sum(1 for w in OFF_TOPIC_WORDS if " " in w and w in low)
    if off and not domain:
        return OFF_TOPIC_REPLY
    return None


# ---------------------------------------------------------------------------
# Database access (read-only)
# ---------------------------------------------------------------------------
def ensure_db() -> None:
    if not os.path.exists(DB_PATH):
        from setup_db import build
        log.warning("superhouse.db missing - building from Excel")
        build(db_path=DB_PATH)


def connect_ro() -> sqlite3.Connection:
    ensure_db()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def run_sql(sql: str, params=()):
    """Execute on a read-only connection with a wall-clock budget. Returns (columns, rows, truncated)."""
    con = connect_ro()
    deadline = time.monotonic() + QUERY_TIME_BUDGET_S
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20_000)
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(MAX_ROWS + 1)
        truncated = len(rows) > MAX_ROWS
        return cols, [tuple(r) for r in rows[:MAX_ROWS]], truncated
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e).lower():
            raise sqlite3.OperationalError(
                f"query exceeded {QUERY_TIME_BUDGET_S:.0f}s time budget - avoid cross joins / "
                "join on keys (order_no, article_no) and aggregate before joining") from e
        raise
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Live schema catalog (Pillar 1: the DB, not a hand-written DDL, is the truth)
# ---------------------------------------------------------------------------
_catalog_cache = {"mtime": None, "text": None}


def schema_catalog() -> str:
    ensure_db()
    mtime = os.path.getmtime(DB_PATH)
    if _catalog_cache["mtime"] == mtime:
        return _catalog_cache["text"]

    con = connect_ro()
    try:
        objs = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE '\\_%' ESCAPE '\\' "
            "AND name NOT LIKE 'sqlite%' ORDER BY type, name")]
        parts = []
        for t in objs:
            desc, coldocs = TABLE_DOCS.get(t, ("", {}))
            n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            parts.append(f"\n### {t}  ({n} rows)\n{desc}")
            if t == "material_stock":
                parts.append("Columns: category + every column of leather_stock.")
                continue
            for cid, name, ctype, *_ in con.execute(f'PRAGMA table_info("{t}")'):
                line = f"- {name} {ctype or 'TEXT'}"
                if coldocs.get(name):
                    line += f" -- {coldocs[name]}"
                if (ctype or "TEXT").upper() == "TEXT":
                    distinct = con.execute(
                        f'SELECT COUNT(DISTINCT "{name}") FROM "{t}"').fetchone()[0]
                    if name.endswith("_date"):
                        lo, hi = con.execute(f'SELECT MIN("{name}"), MAX("{name}") FROM "{t}"').fetchone()
                        line += f"  [range {lo} .. {hi}]"
                    elif 0 < distinct <= 25:
                        vals = [r[0] for r in con.execute(
                            f'SELECT "{name}", COUNT(*) c FROM "{t}" WHERE "{name}" IS NOT NULL '
                            f'GROUP BY 1 ORDER BY c DESC')]
                        line += "  [verified values: " + ", ".join(repr(v) for v in vals) + "]"
                    else:
                        ex = [r[0] for r in con.execute(
                            f'SELECT DISTINCT "{name}" FROM "{t}" WHERE "{name}" IS NOT NULL LIMIT 4')]
                        line += f"  [{distinct} distinct, e.g. " + ", ".join(repr(v) for v in ex) + "]"
                parts.append(line)
        text = "\n".join(parts)
    finally:
        con.close()
    _catalog_cache.update(mtime=mtime, text=text)
    return text


def db_overview() -> dict:
    """Small KPI set for the UI sidebar. Never raises."""
    try:
        con = connect_ro()
        try:
            q = lambda s: con.execute(s).fetchone()
            return {
                "leather_items": q("SELECT COUNT(*) FROM leather_stock")[0],
                "sole_items": q("SELECT COUNT(*) FROM sole_stock")[0],
                "sale_orders": q("SELECT COUNT(DISTINCT order_no) FROM sale_orders")[0],
                "order_pairs": q("SELECT COALESCE(SUM(order_qty),0) FROM sale_orders")[0],
                "samples": q("SELECT COUNT(*) FROM samples")[0],
                "open_samples": q("SELECT COUNT(*) FROM samples WHERE status='Open'")[0],
                "io_count": q("SELECT COUNT(*) FROM io_to_last")[0],
                "io_balance": q("SELECT COALESCE(SUM(bal_qty),0) FROM io_to_last")[0],
                "excess_value": q("SELECT COALESCE(SUM(excess_amt),0) FROM material_stock")[0],
                "built_at": (q("SELECT value FROM _meta WHERE key='built_at_utc'") or [None])[0],
            }
        finally:
            con.close()
    except Exception:
        log.exception("db_overview failed")
        return {}


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
RULES = r"""
## SQL dialect: SQLite 3
- Only ONE read-only statement: SELECT ... or WITH ... SELECT ... . Never INSERT/UPDATE/DELETE/DDL/PRAGMA.
- Dates are ISO TEXT 'YYYY-MM-DD'. TODAY is {today} (IST). Use SQLite date math relative to DATE('{today}'),
  never a date you computed yourself:
    today                 -> col = DATE('{today}')
    yesterday             -> col = DATE('{today}','-1 day')
    last 7 days           -> col >= DATE('{today}','-7 days') AND col <= DATE('{today}')
    this month            -> strftime('%Y-%m', col) = strftime('%Y-%m', '{today}')
    last month            -> strftime('%Y-%m', col) = strftime('%Y-%m', DATE('{today}','start of month','-1 month'))
    next 30 days          -> col BETWEEN DATE('{today}') AND DATE('{today}','+30 days')
    this year             -> strftime('%Y', col) = strftime('%Y', '{today}')
    month name w/o year   -> that month of the current year (previous year if it is in the future)
    week-wise / monthly   -> GROUP BY strftime('%Y-%W', col) / strftime('%Y-%m', col)
- No ILIKE, no RIGHT/FULL JOIN, no DATE_FORMAT/CURDATE/INTERVAL (MySQL). Use || for concat, IFNULL/COALESCE.
- Window functions and CTEs ARE supported.
- Text matching: ALWAYS fuzzy & case-insensitive: col LIKE '%term%'. Split multi-word names into separate
  LIKE conditions ('cow swede black' -> LIKE '%cow%' AND LIKE '%swede%' AND LIKE '%black%').
  Exact '=' only for values listed as [verified values] (use the exact spelling shown).
- Round quantities: ROUND(x, 2). Always give aggregates a readable alias (e.g. total_pairs, excess_value_rs).
- Lists: ORDER BY something meaningful and LIMIT 100 unless the user asks for all / a specific top-N.
- When a partial name matches several items, GROUP BY / list the individual items - don't silently merge them.
- Prefer returning identifying columns (product_name, order_no, style...) alongside numbers.

## Business glossary (Superhouse Limited, Shoe Division, Kanpur)
- MRP sheets (leather_stock, sole_stock, material_stock view): one row per material item.
    "short / shortage / kami"          -> short_fall > 0 (stock can't cover balance requirement)
    "to be ordered / order karna hai / need to purchase" -> to_be_ord > 0 (net of stock + open PO + OSJ)
    "urgent / overdue requirement"     -> age_overdue > 0 (or age_0_30 > 0 for next 30 days)
    "excess / surplus / dead stock"    -> excess_stk > 0 ; value -> excess_amt (Rs.)
    "on order / pending PO"            -> ord_qty > 0
    "out of stock / no stock"          -> cr_stock <= 0 ; "in stock" -> cr_stock > 0
    "below min level"                  -> min_lvl > 0 AND cr_stock < min_lvl
    Leather groups: group_name in the verified list (e.g. 'Cow Swede', 'Imported Leather', 'Split'...).
    Quantity units differ (Sdm./Mtr./Sqm. for leather, Pair/sht. for sole) - don't add different UOMs
    together without grouping by uom.
- sale_orders: every row is an OPEN order line; quantities in pairs; no price/value column exists.
    "shipping / due / ex-factory"      -> ex_factory_date
    "delayed / late / overdue orders"  -> ex_factory_date < DATE('{today}')
    "shipping this month / next N days"-> ex_factory_date date filters
    "CSA pending / not approved"       -> csa_date IS NULL
    "not booked / no booking ref"      -> cur_bk_ref_no IS NULL
    Buyer/brand -> column buyer (derived) OR product_name LIKE '%(Walmart)%'; use LIKE on buyer
    (spelling variants exist, e.g. 'Patrick'/'Patricks'). When grouping by buyer use
    COALESCE(buyer, '(not specified)') AS buyer.
    Rupee orders: currency IN ('INR','Rs.').
    UK destinations are spelled many ways: destination LIKE '%U.K%' OR LIKE '%Uk%' OR LIKE '%Felixstowe%'
    OR LIKE '%Southampton%'. USA: LIKE '%U.S.A%' OR LIKE '%Usa%'. Germany/Belgium: LIKE '%Antwerp%'.
    Number of orders -> COUNT(DISTINCT order_no) (an order can have several lines).
    "pending / open sale orders"       -> order_status = 'Open' (every order line in this report is open/pending
                                          shipment; do NOT add an ex_factory_date filter unless the user asks).
- samples: sample register.
    Development samples -> sample_type = 'Devlopment' (ERP spelling!). Selection -> 'Slection'.
    Confirmation incl. revised -> sample_type LIKE 'Confirmation%'. PPS -> 'Pps'.
    "pending / open samples"           -> status = 'Open'
    "late / overdue samples"           -> status = 'Open' AND delivery_date < DATE('{today}')
    "completed on time"                -> status='Completed' AND status_date <= delivery_date
    "turnaround / lead time days"      -> julianday(status_date) - julianday(doc_date) for Completed
- io_to_last: internal orders pending for cutting/lasting.
    "pending / balance to cut"         -> bal_qty (io_qty - taken_qty); "not started" -> taken_qty = 0
    "progress %"                       -> ROUND(100.0 * taken_qty / io_qty, 1)
    Readiness flags grd, trl, m_chk, c_d hold 'Yes'/'No'.
- Merchandisers (people): Tanmay, Fazil, Rajeev, Musheer, Waqar Javed, Chandan Rathi, Kaif, Tajwar Akhtar.
  Match with LIKE (io_to_last uses 'Waqar').

## Cross-sheet mapping
- sale_orders.order_no = io_to_last.order_no              (order -> its internal order / production status)
- article_no links sale_orders, samples, io_to_last        (article -> orders, samples, IO)
- Leather names in samples.leather / io_to_last.leather are short ('Cow Swiss', 'cow Swede'); to relate to
  stock use material_stock.product_name LIKE '%' || leather || '%' (or split words), and aggregate stock first.
- Always aggregate each side in a subquery BEFORE joining one-to-many tables, to avoid double counting.

## Output format - reply with ONE JSON object, nothing else
{{"type": "sql", "sql": "<SQLite query>", "title": "<3-6 word title for the result>"}}
{{"type": "chat", "message": "<short reply>"}}      -- greetings / meta questions about what you can do
{{"type": "clarify", "message": "<one question>"}}  -- ONLY when truly impossible to guess; prefer a sensible default
Answer every question that can be derived from the data with SQL, including analytics, rankings,
percentages, comparisons, trends and cross-sheet questions. Users may write in English, Hindi or Hinglish.
Questions that cannot be answered from these tables (prices of sale orders, supplier names, weather...) ->
type "chat" explaining briefly what data is available.
"""

GEN_UI_RULES = """
## Gen UI (dashboard) mode is ON
Shape the result so it charts well: one label column (category or date) + one or more numeric measure columns,
already aggregated, ordered meaningfully (ranking DESC, or chronological ASC for dates), max ~25 rows.
Add a "chart" key to the JSON:
  "chart": {"type": "bar" | "line" | "kpi", "x": "<label column alias>", "y": "<main numeric column alias>"}
  bar  = compare categories/items; line = trend over dates/months (x must be the date/month column);
  kpi  = a single row of headline numbers (x/y may be null).
"""

ANSWER_PROMPT = """You are the Superhouse Limited data assistant. The full query result is displayed to the user
as a table directly below your text. Write ONLY a professional opening of 1-2 short sentences
(max 35 words) that introduces the result, e.g. "Here are the top 5 leather items by balance quantity:".
Rules:
- NEVER list rows, NEVER use bullet points, NEVER repeat the table, NEVER output markdown tables.
- If the result is a single value or a single row, state that headline figure in the sentence (in **bold**).
- Never invent numbers. Mention truncation if the result was truncated.
- Match the language style of THIS question (English or Hinglish). No SQL.
"""


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def system_prompt(gen_ui: bool = False) -> str:
    return ("You are an expert SQLite analyst for Superhouse Limited (Shoe Division). You convert business "
            "questions into ONE correct SQLite query over the database below.\n\n## Live database schema"
            + schema_catalog() + "\n" + RULES.format(today=_today()) + (GEN_UI_RULES if gen_ui else ""))


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------
_client = None


def _api_key():
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    return key if key and "your_openai" not in key else None


def llm_ready() -> bool:
    return _api_key() is not None


def _get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(api_key=_api_key(), timeout=60, max_retries=2)
    return _client


_NO_TEMPERATURE = {"o1", "o3", "o4", "gpt-5", "gpt-6"}     # model families known to reject temperature=0


def _complete(messages, json_mode=False) -> str:
    kwargs = {"model": MODEL, "messages": messages}
    if not MODEL.startswith(tuple(_NO_TEMPERATURE)):
        kwargs["temperature"] = 0
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = _get_client().chat.completions.create(**kwargs)
    except Exception as e:
        # A newer model may reject temperature; retry once without it instead of failing the question.
        if "temperature" in kwargs and "temperature" in str(e).lower():
            log.warning("model %s rejected temperature; retrying without it", MODEL)
            kwargs.pop("temperature")
            resp = _get_client().chat.completions.create(**kwargs)
        else:
            raise
    return resp.choices[0].message.content or ""


def _history_messages(history):
    msgs = []
    for turn in (history or [])[-HISTORY_TURNS:]:
        if turn.get("role") == "user":
            msgs.append({"role": "user", "content": turn.get("content", "")})
        elif turn.get("role") == "assistant":
            note = turn.get("content", "")
            if turn.get("sql"):
                note += f"\n[SQL used: {turn['sql']}]"
            msgs.append({"role": "assistant", "content": note[:1500]})
    return msgs


def generate_sql(question, history=None, previous_sql=None, error=None, zero_rows=False, gen_ui=False) -> dict:
    messages = [{"role": "system", "content": system_prompt(gen_ui)}]
    messages += _history_messages(history)
    if previous_sql and error:
        user = (f"Question: {question}\n\nYour previous SQL failed.\nSQL:\n{previous_sql}\n\nSQLite error:\n{error}\n\n"
                "Fix it. Re-check every table/column name against the live schema above, use only SQLite syntax, "
                "and return the corrected JSON.")
    elif previous_sql and zero_rows:
        user = (f"Question: {question}\n\nThis SQL ran but returned 0 rows (or only NULLs):\n{previous_sql}\n\n"
                "Re-check: literal values must match the [verified values] exactly; names should use LIKE with "
                "wildcards and split words; date filters must use the right column and TODAY; do not over-filter. "
                "If the query is already correct and the answer really is empty, return exactly the same SQL.")
    else:
        user = question
    messages.append({"role": "user", "content": user})
    raw = _complete(messages, json_mode=True)
    return _parse_json(raw)


def _parse_json(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw or "", re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except json.JSONDecodeError:
                data = None
        else:
            data = None
        if data is None:
            m = re.search(r"(?is)\b(select|with)\b.+", raw or "")
            return {"type": "sql", "sql": m.group(0).strip("` \n")} if m else {"type": "chat", "message": raw}
    if not isinstance(data, dict):
        return {"type": "chat", "message": str(data)}
    return data


# ---------------------------------------------------------------------------
# Read-only guard
# ---------------------------------------------------------------------------
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|pragma|vacuum|reindex|truncate|grant|revoke|"
    r"replace\s+into|load_extension)\b", re.I)


def clean_sql(sql: str) -> str:
    sql = (sql or "").strip()
    sql = re.sub(r"^```(?:sql)?|```$", "", sql, flags=re.I).strip()
    sql = re.sub(r"--[^\n]*", "", sql)
    sql = re.sub(r"/\*.*?\*/", "", sql, flags=re.S)
    return sql.strip().rstrip(";").strip()


def is_safe_sql(sql: str) -> bool:
    if not sql:
        return False
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", sql)
    if ";" in no_strings:
        return False                                  # multiple statements
    if not re.match(r"^\s*(select|with|\()", no_strings, re.I):
        return False
    return not FORBIDDEN.search(no_strings)


def _is_empty(rows) -> bool:
    if not rows:
        return True
    if len(rows) == 1 and all(v is None for v in rows[0]):
        return True
    return False


# ---------------------------------------------------------------------------
# Answer writing
# ---------------------------------------------------------------------------
def _fmt(v):
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"{int(v):,}"
    if isinstance(v, int):
        return f"{v:,}"
    return "" if v is None else str(v)


MD_TABLE_LINE = re.compile(r"^\s*\|.*\|\s*$")


def strip_md_tables(text: str) -> str:
    """The UI renders the real result table; drop any markdown table the LLM adds anyway."""
    lines = [ln for ln in (text or "").splitlines() if not MD_TABLE_LINE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


BULLET_LINE = re.compile(r"^\s*([-*•]|\d+[.)])\s+")
SENTENCE_END = re.compile(r"(?<=[.!?:])\s+(?=[A-Z*\"'(])")


def opening_only(text: str, max_sentences: int = 2) -> str:
    """Hard guarantee of 'no double answers': keep the first 1-2 sentences, drop lists/tables."""
    text = strip_md_tables(text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not BULLET_LINE.match(ln)]
    flat = " ".join(lines)
    return " ".join(SENTENCE_END.split(flat)[:max_sentences]).strip()


def default_opening(title, rows, truncated) -> str:
    what = f" for **{title}**" if title else ""
    if len(rows) == 1:
        return f"Here is the result{what}:"
    more = f"the first {len(rows):,}" if truncated else f"{len(rows):,}"
    return f"Here are the details{what} ({more} records):"


def write_answer(question, sql, cols, rows, truncated, history=None, title=None) -> str:
    sample = [dict(zip(cols, (_fmt(v) for v in r))) for r in rows[:SUMMARY_ROWS]]
    info = (f"Question: {question}\nSQL: {sql}\nTotal rows returned: {len(rows)}"
            f"{' (truncated at ' + str(MAX_ROWS) + ')' if truncated else ''}\n"
            f"Rows (first {len(sample)}):\n{json.dumps(sample, ensure_ascii=False)}")
    try:
        msgs = [{"role": "system", "content": ANSWER_PROMPT}]
        msgs.append({"role": "user", "content": info})
        text = opening_only(_complete(msgs))
        return text or default_opening(title, rows, truncated)
    except Exception:
        log.exception("answer writing failed")
        return default_opening(title, rows, truncated)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def chart_spec(plan: dict, cols, rows):
    """Validate the LLM's chart hint against the real result; fall back to a heuristic. Never raises."""
    try:
        numeric = [c for i, c in enumerate(cols)
                   if any(_is_number(r[i]) for r in rows) and all(r[i] is None or _is_number(r[i]) for r in rows)]
        labels = [c for c in cols if c not in numeric]
        hint = plan.get("chart") if isinstance(plan.get("chart"), dict) else {}
        ctype = str(hint.get("type") or "").lower()
        x = hint.get("x") if hint.get("x") in labels else (labels[0] if labels else None)
        y = hint.get("y") if hint.get("y") in numeric else (numeric[-1] if numeric else None)
        if not numeric:
            return None
        if len(rows) == 1:
            ctype = "kpi"
        elif ctype not in ("bar", "line", "kpi"):
            first = str(rows[0][cols.index(x)]) if x else ""
            ctype = "line" if re.match(r"^\d{4}-\d{2}", first) else "bar"
        if ctype != "kpi" and not x:
            ctype = "kpi"
        return {"type": ctype, "x": x, "y": y, "measures": numeric}
    except Exception:
        log.exception("chart spec failed")
        return None


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _reply(kind, message, **extra):
    out = {"type": kind, "message": message, "sql": None, "columns": [], "rows": [],
           "truncated": False, "title": None, "attempts": 0, "chart": None}
    out.update(extra)
    return out


def ask(question: str, history=None, gen_ui: bool = False) -> dict:
    """Answer a natural-language question. Never raises."""
    try:
        question = (question or "").strip()
        if not question:
            return _reply("chat", GREETING_REPLY)
        if len(question) > 1000:
            return _reply("chat", "That question is a bit long - please keep it under 1000 characters.")

        canned = firewall(question, has_history=bool(history))
        if canned:
            return _reply("chat", canned)
        if not llm_ready():
            return _reply("error", NO_KEY_MSG)

        plan = generate_sql(question, history, gen_ui=gen_ui)
        kind = (plan.get("type") or "sql").lower()
        if kind in ("chat", "clarify"):
            return _reply("chat", plan.get("message") or GREETING_REPLY)

        sql = clean_sql(plan.get("sql"))
        title = plan.get("title")
        soft_healed = False
        attempts = 0
        cols, rows, truncated = [], [], False
        for attempt in range(MAX_REPAIRS + 1):            # 1 original + 3 repairs
            attempts = attempt + 1
            if not is_safe_sql(sql):
                log.warning("[guard] rejected SQL: %s", (sql or "")[:200])
                return _reply("chat", "I couldn't build a safe query for that. Please ask it a bit more specifically.",
                              attempts=attempts)
            try:
                cols, rows, truncated = run_sql(sql)
            except Exception as err:
                log.warning("[self-heal] attempt %d failed: %s | SQL: %s", attempt + 1, str(err)[:200], sql[:300])
                if attempt == MAX_REPAIRS:
                    return _reply("error", GAVE_UP_MSG, sql=sql, attempts=attempts)
                fix = generate_sql(question, history, previous_sql=sql, error=str(err), gen_ui=gen_ui)
                if (fix.get("type") or "sql").lower() != "sql":
                    return _reply("chat", fix.get("message") or GAVE_UP_MSG, attempts=attempts)
                sql = clean_sql(fix.get("sql"))
                title = fix.get("title") or title
                plan = fix if fix.get("chart") else plan
                continue

            # Soft heal: a valid query that returns nothing is often a wrong literal / over-strict filter.
            if _is_empty(rows) and not soft_healed and attempt < MAX_REPAIRS:
                soft_healed = True
                fix = generate_sql(question, history, previous_sql=sql, zero_rows=True, gen_ui=gen_ui)
                new_sql = clean_sql(fix.get("sql")) if (fix.get("type") or "sql").lower() == "sql" else ""
                if new_sql and new_sql != sql:
                    log.info("[self-heal] zero rows -> retrying with revised SQL")
                    sql, title = new_sql, fix.get("title") or title
                    plan = fix if fix.get("chart") else plan
                    continue
            break

        if _is_empty(rows):
            return _reply("no_records", NO_RECORD_MSG, sql=sql, columns=cols, attempts=attempts, title=title)

        answer = write_answer(question, sql, cols, rows, truncated, history, title=title)
        return _reply("answer", answer, sql=sql, columns=cols, rows=rows, truncated=truncated,
                      title=title, attempts=attempts, chart=chart_spec(plan, cols, rows) if gen_ui else None)
    except Exception as e:
        log.exception("chatbot pipeline failed")
        msg = GENERIC_FAIL_MSG
        name = type(e).__name__
        if name in ("AuthenticationError", "PermissionDeniedError"):
            msg = NO_KEY_MSG
        elif name == "RateLimitError":
            msg = "The AI service is rate-limited or out of quota right now. Please try again shortly."
        elif name in ("APIConnectionError", "APITimeoutError"):
            msg = "I couldn't reach the AI service. Please check the connection and try again."
        return _reply("error", msg)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    q = " ".join(sys.argv[1:]) or "Which leather items need to be ordered?"
    res = ask(q)
    print(res["message"])
    if res["sql"]:
        print("\nSQL:", res["sql"])
        print(res["columns"])
        for r in res["rows"][:10]:
            print(r)
