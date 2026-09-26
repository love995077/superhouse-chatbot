"""
Automated tests for the Superhouse NL2SQL engine, run directly against the local superhouse.db.

    python test_bot.py            # offline checks + the 3 live AI queries (needs OPENAI_API_KEY)
    python test_bot.py --offline  # offline checks only (no API calls)

Live tests compare the bot's returned rows with independent ground-truth SQL written by hand,
so a query that "runs" but returns the wrong data fails the test.
"""
import json
import logging
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chatbot  # noqa: E402

DB = chatbot.DB_PATH
RESULTS = []


def truth(sql, params=()):
    con = sqlite3.connect(DB)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def column_matching(cols, rows, expected_values, numeric=False, tol=0.05):
    """Index of the result column whose values equal expected_values (order-insensitive)."""
    for i, _ in enumerate(cols):
        got = [r[i] for r in rows]
        if numeric:
            try:
                if len(got) == len(expected_values) and all(
                        abs(float(g) - float(e)) <= tol for g, e in zip(sorted(got), sorted(expected_values))):
                    return i
            except (TypeError, ValueError):
                continue
        elif sorted(map(str, got)) == sorted(map(str, expected_values)):
            return i
    return None


def show(cols, rows, limit=10):
    widths = [max(len(str(c)), *(len(chatbot._fmt(r[i])) for r in rows[:limit])) for i, c in enumerate(cols)]
    line = lambda vals: "    | " + " | ".join(str(v).ljust(w)[:40] for v, w in zip(vals, widths)) + " |"
    print(line(cols))
    for r in rows[:limit]:
        print(line([chatbot._fmt(v) for v in r]))
    if len(rows) > limit:
        print(f"    ... {len(rows) - limit} more rows")


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{' - ' + detail if detail else ''}")


# ---------------------------------------------------------------------------
# Offline tests (no API calls)
# ---------------------------------------------------------------------------
def offline_tests():
    print("\n== Offline checks ==")
    # Database reconciles with the Excel footer totals (Pillar 2)
    cr, amt = truth("SELECT ROUND(SUM(cr_stock),3), ROUND(SUM(excess_amt),2) FROM leather_stock")[0]
    record("Leather totals reconcile with Excel", (cr, amt) == (2137297.087, 11822747.39), f"{cr} / {amt}")
    io = truth("SELECT SUM(io_qty), SUM(taken_qty), SUM(bal_qty) FROM io_to_last")[0]
    record("IO totals reconcile with Excel", tuple(int(v) for v in io) == (15393, 4294, 11099), str(io))

    # Read-only guard
    unsafe = ["DELETE FROM samples", "select 1; drop table x", "UPDATE sale_orders SET order_qty=0", "PRAGMA table_info(x)",
              "ATTACH DATABASE 'x' AS y"]
    safe = ["SELECT 1", "WITH a AS (SELECT 1) SELECT * FROM a", "SELECT * FROM samples WHERE style_name LIKE '%update;%'"]
    record("Guard blocks write/DDL statements", not any(chatbot.is_safe_sql(chatbot.clean_sql(s)) for s in unsafe))
    record("Guard allows read-only statements", all(chatbot.is_safe_sql(chatbot.clean_sql(s)) for s in safe))

    # Off-topic firewall
    record("Firewall answers greetings", chatbot.firewall("hello", False) is not None)
    record("Firewall blocks off-topic", chatbot.firewall("tell me a cricket joke", False) is not None)
    record("Firewall lets ERP questions through", chatbot.firewall("Walmart orders this month", False) is None)

    # "No double answers": opener is cut to max 2 sentences, lists/tables stripped
    op = chatbot.opening_only("Here are the details: Zign leads. Next sentence. Third one.\n- bullet\n| a | b |")
    record("Opening capped at 2 sentences, no bullets/tables", op.count(".") <= 2 and "-" not in op and "|" not in op, repr(op))

    # Self-healing loop with a scripted fake LLM (bad column -> bad column -> fixed)
    real_complete, real_key = chatbot._complete, os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = real_key if chatbot.llm_ready() else "sk-offline-test"
    script = [
        json.dumps({"type": "sql", "sql": "SELECT product_nm FROM leather_stock"}),
        json.dumps({"type": "sql", "sql": "SELECT product_name, bal_quantity FROM leather_stock"}),
        json.dumps({"type": "sql", "sql": "SELECT product_name, bal_qty FROM leather_stock ORDER BY bal_qty DESC LIMIT 3"}),
        "Here are the top 3 leather items by balance quantity:",
    ]
    chatbot._complete = lambda messages, json_mode=False: script.pop(0)
    logging.getLogger("superhouse.chatbot").disabled = True     # simulated failures are expected here
    try:
        r = chatbot.ask("top 3 leather by balance")
        record("Self-heal repairs 2 broken SQLs (3rd attempt succeeds)", r["type"] == "answer" and r["attempts"] == 3 and len(r["rows"]) == 3,
               f"type={r['type']} attempts={r['attempts']}")
        script[:] = [json.dumps({"type": "sql", "sql": "SELECT nope FROM samples"})] * 4
        r = chatbot.ask("sample count")
        record("Gives up cleanly after 3 repairs (no crash)", r["type"] == "error" and r["attempts"] == 4)
        script[:] = [json.dumps({"type": "sql", "sql": "SELECT * FROM sale_orders WHERE buyer = 'NOBODY'"})] * 2
        r = chatbot.ask("orders of buyer nobody")
        record("Zero rows -> explicit 'no records' message", r["type"] == "no_records" and bool(r["message"]))
        chatbot._complete = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network down"))
        r = chatbot.ask("leather stock total")
        record("LLM outage -> polite error, no exception", r["type"] == "error" and bool(r["message"]))
    finally:
        logging.getLogger("superhouse.chatbot").disabled = False
        chatbot._complete = real_complete
        if real_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = real_key


# ---------------------------------------------------------------------------
# Live AI tests (the 3 client queries)
# ---------------------------------------------------------------------------
def live_query(question):
    t = time.time()
    r = chatbot.ask(question)
    print(f"\n  Q: {question}\n  A: {r['message']}\n  ({r['type']}, {len(r['rows'])} rows, attempts={r['attempts']}, {time.time() - t:.1f}s)")
    print(f"  SQL: {' '.join((r['sql'] or '').split())}")
    if r["rows"]:
        show(r["columns"], r["rows"])
    return r


def live_tests():
    print("\n== Live AI tests (OpenAI -> SQL -> superhouse.db) ==")
    if not chatbot.llm_ready():
        record("Live tests", False, "OPENAI_API_KEY not configured")
        return

    # 1. Top 5 leather items by balance quantity
    exp = truth("SELECT product_name, ROUND(bal_qty, 2) FROM leather_stock ORDER BY bal_qty DESC LIMIT 5")
    r = live_query("Show me the top 5 leather items with the highest balance quantity.")
    ni = column_matching(r["columns"], r["rows"], [e[0] for e in exp])
    qi = column_matching(r["columns"], r["rows"], [e[1] for e in exp], numeric=True)
    order_ok = ni is not None and [row[ni] for row in r["rows"]] == [e[0] for e in exp]
    record("Q1 top-5 leather by balance: exact items, values and order",
           r["type"] == "answer" and len(r["rows"]) == 5 and order_ok and qi is not None,
           f"expected {[e[0] for e in exp]}")

    # 2. Pending sale orders for Walmart (every order line in the report is open)
    exp = truth("SELECT order_no FROM sale_orders WHERE order_status = 'Open' "
                "AND (buyer LIKE '%walmart%' OR product_name LIKE '%walmart%')")
    r = live_query("List the pending sale orders for Walmart.")
    oi = column_matching(r["columns"], r["rows"], [e[0] for e in exp])
    record("Q2 pending Walmart sale orders: exact order set",
           r["type"] == "answer" and oi is not None, f"expected {len(exp)} order lines")

    # 3. IO progress for style 22467
    exp = truth("SELECT io_no, io_qty, taken_qty, bal_qty, ROUND(100.0 * taken_qty / io_qty, 1) "
                "FROM io_to_last WHERE article_no = '22467' OR style LIKE '%22467%'")
    r = live_query("What is the IO progress for style 22467?")
    ii = column_matching(r["columns"], r["rows"], [e[0] for e in exp])
    progress_ok = (column_matching(r["columns"], r["rows"], [e[4] for e in exp], numeric=True, tol=0.15) is not None
                   or column_matching(r["columns"], r["rows"], [e[2] for e in exp], numeric=True) is not None
                   or column_matching(r["columns"], r["rows"], [e[3] for e in exp], numeric=True) is not None)
    record("Q3 IO progress for style 22467: exact IOs + progress figures",
           r["type"] == "answer" and ii is not None and progress_ok, f"expected IOs {[e[0] for e in exp]}")

    # "No double answers" on real output
    record("Answers are a short opening only (<= 2 sentences, no bullets)",
           all(len(chatbot.SENTENCE_END.split(m)) <= 2 and "\n-" not in m for m in [r["message"]]))


if __name__ == "__main__":
    chatbot.ensure_db()
    offline_tests()
    if "--offline" not in sys.argv:
        live_tests()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n== {passed}/{len(RESULTS)} checks passed ==")
    sys.exit(0 if passed == len(RESULTS) else 1)
