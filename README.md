# Superhouse Chatbot

AI assistant for **Superhouse Limited (Shoe Division)**. Ask questions in English or Hinglish about material stock, shortfalls, sale orders, samples and internal orders, and get answers straight from the ERP export (`Chatbot data.xlsx`).

Built with Streamlit, the OpenAI API and SQLite, following the *NL2SQL Architect* methodology:

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

## Run locally

```bash
python -m venv venv
venv\Scripts\activate            # Windows  (source venv/bin/activate on Linux/Mac)
pip install -r requirements.txt
copy .env.example .env           # then put your real OPENAI_API_KEY in .env
python setup_db.py               # builds superhouse.db and prints reconciliation
streamlit run app.py
```

Replace the placeholder logo by dropping the official logo into the project root as `logo.png`. The default mark lives in `assets/logo.svg`.

## Deploy to Render

```bash
# .env must contain RENDER_API_KEY (and ideally a real OPENAI_API_KEY)
python deploy_render.py
```

The script creates the web service if it doesn't exist, or redeploys it if it does. The service runs:
- **Build command:** `pip install -r requirements.txt && python setup_db.py`
- **Start command:** `streamlit run app.py --server.port $PORT --server.address 0.0.0.0`

To refresh the data, replace `Chatbot data.xlsx` and push. Render rebuilds the database automatically.
