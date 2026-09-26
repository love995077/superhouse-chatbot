"""
Build superhouse.db (SQLite) from "Chatbot data.xlsx".

Workbook layout (verified against the real file):
    row 0  -> "Superhouse Limited (Shoe Division)"   (banner)
    row 1  -> report title ("M.R.P.", "Sale Order", "Sample", "I.O.To Cut")
    row 2  -> the real column headers
    ...    -> data
    last   -> "< Total >" / "Grand Total" footer on the order sheets

The header row is auto-detected per sheet (HEADER_ANCHORS) so a shifted sheet
won't silently load junk. Footer/total rows and the MRP group-header rows are
removed so SUM() in SQL reproduces the Excel totals exactly.

Usage:  python setup_db.py [path/to/workbook.xlsx] [path/to/output.db]
"""
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

import pandas as pd

from schema_docs import (
    COLUMN_MAPS, DATE_COLUMNS, HEADER_ANCHORS, SHEETS, TEXT_ID_COLUMNS, ZERO_FILL_COLUMNS,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XLSX = os.path.join(BASE_DIR, "Chatbot data.xlsx")
DEFAULT_DB = os.path.join(BASE_DIR, "superhouse.db")

TOTAL_ROW = re.compile(r"(grand\s*total|<\s*total\s*>|^total$)", re.I)
ARTICLE = re.compile(r"^\s*([A-Za-z]{0,3}-?\d{3,})")
LAST_PARENS = re.compile(r"\(([^()]*)\)(?!.*\()")   # last "(...)" even if a ",Suffix" follows


def slugify(header: str) -> str:
    s = str(header).strip().lower().replace("<", " lt ").replace(">", " gt ")
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s if s and not s[0].isdigit() else f"col_{s}"


def find_header_row(raw: pd.DataFrame, anchor: str, default: int = 1) -> int:
    for i in range(min(15, len(raw))):
        if any(str(v).strip() == anchor for v in raw.iloc[i].tolist()):
            return i
    return default


def load_sheet(xls: pd.ExcelFile, sheet: str, table: str) -> pd.DataFrame:
    raw = pd.read_excel(xls, sheet, header=None)
    hdr = find_header_row(raw, HEADER_ANCHORS[sheet])
    df = raw.iloc[hdr + 1:].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr].tolist()]
    df = df.loc[:, [c for c in df.columns if c and c.lower() != "nan"]]

    mapping = COLUMN_MAPS[table]
    df.columns = [mapping.get(c, slugify(c)) for c in df.columns]
    df = df.dropna(how="all")

    # Strip whitespace from every text cell ('29134        ' -> '29134').
    for c in df.columns:
        df[c] = df[c].map(lambda v: (v.strip() or None) if isinstance(v, str) else v)

    first = df.columns[0]
    # Drop footer total rows ("< Total >", "Grand Total") wherever they appear.
    is_total = df.apply(lambda r: any(isinstance(v, str) and TOTAL_ROW.search(v) for v in r.tolist()[:3]), axis=1)
    df = df[~is_total]

    if table in ("leather_stock", "sole_stock"):
        # Group-header rows have a name but no UOM; they carry sub-totals.
        is_group = df["uom"].isna()
        df.insert(0, "group_name", df["product_name"].where(is_group).ffill())
        df = df[~is_group]

    for c in ZERO_FILL_COLUMNS.get(table, []):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    for c in TEXT_ID_COLUMNS.get(table, []):
        if c in df.columns:
            df[c] = df[c].map(_id_to_text)

    for c in DATE_COLUMNS.get(table, []):
        if c in df.columns:
            d = pd.to_datetime(df[c], errors="coerce", dayfirst=True, format="mixed")
            df[c] = d.dt.strftime("%Y-%m-%d").where(d.notna(), None)

    # Derived cross-sheet keys.
    src = {"sale_orders": "product_name", "samples": "style_name", "io_to_last": "style"}.get(table)
    if src:
        df.insert(df.columns.get_loc(src) + 1, "article_no", df[src].map(_article))
    if table == "sale_orders":
        df.insert(df.columns.get_loc("article_no") + 1, "buyer", df["product_name"].map(_buyer))

    # Numeric columns that came through as object dtype.
    for c in df.columns:
        if df[c].dtype == object and c not in DATE_COLUMNS.get(table, []) and c not in TEXT_ID_COLUMNS.get(table, []):
            conv = pd.to_numeric(df[c], errors="coerce")
            if conv.notna().sum() == df[c].notna().sum() and df[c].notna().any():
                df[c] = conv
    if first == "s_no":
        df[first] = pd.to_numeric(df[first], errors="coerce").astype("Int64")
    return df.reset_index(drop=True)


def _id_to_text(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip() or None


def _article(v):
    if not isinstance(v, str):
        return None
    m = ARTICLE.match(v)
    return m.group(1).upper() if m else None


def _buyer(v):
    if not isinstance(v, str):
        return None
    m = LAST_PARENS.search(v)
    return m.group(1).strip() if m else None


def build(xlsx_path: str = DEFAULT_XLSX, db_path: str = DEFAULT_DB) -> dict:
    if not os.path.exists(xlsx_path):
        raise FileNotFoundError(f"Workbook not found: {xlsx_path}")
    xls = pd.ExcelFile(xlsx_path)
    missing = [s for s in SHEETS if s not in xls.sheet_names]
    if missing:
        raise ValueError(f"Workbook is missing sheets: {missing} (found {xls.sheet_names})")

    tmp_path = db_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    con = sqlite3.connect(tmp_path)
    counts = {}
    try:
        for sheet, table in SHEETS.items():
            df = load_sheet(xls, sheet, table)
            df.to_sql(table, con, index=False)
            counts[table] = len(df)

        con.executescript("""
            CREATE VIEW material_stock AS
                SELECT 'Leather' AS category, * FROM leather_stock
                UNION ALL
                SELECT 'Sole' AS category, * FROM sole_stock;
            CREATE INDEX ix_so_order ON sale_orders(order_no);
            CREATE INDEX ix_so_article ON sale_orders(article_no);
            CREATE INDEX ix_so_date ON sale_orders(order_date);
            CREATE INDEX ix_smp_article ON samples(article_no);
            CREATE INDEX ix_smp_date ON samples(doc_date);
            CREATE INDEX ix_io_order ON io_to_last(order_no);
            CREATE INDEX ix_io_article ON io_to_last(article_no);
            CREATE TABLE _meta (key TEXT PRIMARY KEY, value TEXT);
        """)
        con.executemany("INSERT INTO _meta VALUES (?, ?)", [
            ("source_file", os.path.basename(xlsx_path)),
            ("built_at_utc", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
            *[(f"rows_{t}", str(n)) for t, n in counts.items()],
        ])
        con.commit()
    finally:
        con.close()
    os.replace(tmp_path, db_path)
    return counts


def reconcile(db_path: str = DEFAULT_DB) -> None:
    """Print totals that must match the Excel footer / group-total rows."""
    con = sqlite3.connect(db_path)
    q = lambda sql: con.execute(sql).fetchone()
    print("Reconciliation against Excel totals:")
    print("  Leather  SUM(cr_stock)=%.3f  SUM(excess_amt)=%.2f   (Excel: 2137297.087 / 11822747.39)"
          % q("SELECT SUM(cr_stock), SUM(excess_amt) FROM leather_stock"))
    print("  Sole     SUM(cr_stock)=%.3f  SUM(excess_amt)=%.2f      (Excel: 18175.976 / 164922.64)"
          % q("SELECT SUM(cr_stock), SUM(excess_amt) FROM sole_stock"))
    print("  IO       SUM(io_qty)=%d SUM(taken_qty)=%d SUM(bal_qty)=%d             (Excel: 15393 / 4294 / 11099)"
          % q("SELECT SUM(io_qty), SUM(taken_qty), SUM(bal_qty) FROM io_to_last"))
    print("  Samples  SUM(sample_qty)=%.1f                                   (Excel: 2785.5)"
          % q("SELECT SUM(sample_qty) FROM samples"))
    con.close()


if __name__ == "__main__":
    xlsx = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    db = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_DB
    rows = build(xlsx, db)
    print(f"Built {db}")
    for t, n in rows.items():
        print(f"  {t:<14} {n:>5} rows")
    reconcile(db)
