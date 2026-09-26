"""
Single source of truth for how the Excel sheets map onto SQLite tables,
and for the business meaning of every column.

setup_db.py uses the *_COLUMNS maps to build superhouse.db.
chatbot.py joins the descriptions below with LIVE introspection of the DB
(PRAGMA table_info + verified DISTINCT values), so the LLM prompt can never
drift away from what is actually stored.
"""

# ---------------------------------------------------------------------------
# Excel sheet  ->  SQLite table
# ---------------------------------------------------------------------------
SHEETS = {
    "Leather": "leather_stock",
    "Sole": "sole_stock",
    "Sale order": "sale_orders",
    "sample": "samples",
    "io to last": "io_to_last",
}

# A header cell that must appear in the real header row of each sheet.
# The workbook has a company banner + report title above the header, so the
# header row is detected rather than hard-coded.
HEADER_ANCHORS = {
    "Leather": "Group/Product Name",
    "Sole": "Group/Product Name",
    "Sale order": "Order No.",
    "sample": "Doc. No.",
    "io to last": "I.O.No.",
}

# ---------------------------------------------------------------------------
# Excel header  ->  SQLite column   (any unmapped header is slugified)
# ---------------------------------------------------------------------------
MRP_COLUMNS = {
    "Group/Product Name": "product_name",
    "Uom": "uom",
    "Req.Qty.": "req_qty",
    "Iss.Qty.": "iss_qty",
    "Excess Iss.": "excess_iss",
    "Bal.Qty.": "bal_qty",
    "Cr.Stock": "cr_stock",
    "Short Fall": "short_fall",
    "Excess Stk.": "excess_stk",
    "Excess Amt.": "excess_amt",
    "Ord.Qty.": "ord_qty",
    "OSJ Qty.": "osj_qty",
    "Excess Qty.": "excess_qty",
    "To be Ord.": "to_be_ord",
    "Min.Lvl.": "min_lvl",
    "TBO(AML)": "tbo_aml",
    "< 0 days": "age_overdue",
    "0-30 days": "age_0_30",
    "31-60 days": "age_31_60",
    "61-90 days": "age_61_90",
    "91-120 days": "age_91_120",
    "121-150 days": "age_121_150",
    "> 151 days": "age_151_plus",
}

SALE_ORDER_COLUMNS = {
    "S.No.": "s_no",
    "Order No.": "order_no",
    "Order Date": "order_date",
    "Mer.Name": "merchandiser",
    "Destination": "destination",
    "MOS": "mode_of_shipment",
    "Terms": "terms",
    "Curr.": "currency",
    "Ex.Fact. Date": "ex_factory_date",
    "Order Status": "order_status",
    "Product Name": "product_name",
    "Uom": "uom",
    "Order Qty": "order_qty",
    "CSA. Dt.": "csa_date",
    "Cur.Bk.Ref.No.": "cur_bk_ref_no",
}

SAMPLE_COLUMNS = {
    "S.No.": "s_no",
    "Doc. No.": "doc_no",
    "Doc. Date": "doc_date",
    "Smp.Type": "sample_type",
    "Mer.Name": "merchandiser",
    "Del.Dt.": "delivery_date",
    "Style Name": "style_name",
    "Uom": "uom",
    "Smp.Qty.": "sample_qty",
    "Class": "product_class",
    "Colour": "colour",
    "Last": "shoe_last",
    "Leather": "leather",
    "Lining": "lining",
    "Status": "status",
    "Status Date": "status_date",
    "Disp.Dt.": "dispatch_date",
    "User": "entered_by",
    "Ent. Date": "entry_date",
}

IO_COLUMNS = {
    "I.O.No.": "io_no",
    "Order No.": "order_no",
    "Order Date": "order_date",
    "Buyer": "buyer",
    "Merchandiser": "merchandiser",
    "Ex.Fac.Date": "ex_factory_date",
    "Style": "style",
    "Colour": "colour",
    "Last": "shoe_last",
    "Conv": "conveyor",
    "Leather": "leather",
    "Qtr.Lining": "qtr_lining",
    "Grd.": "grd",
    "Trl.": "trl",
    "M.Chk.": "m_chk",
    "C.D.": "c_d",
    "CSA Date": "csa_date",
    "I.O.Qty.": "io_qty",
    "Tkn.Qty.": "taken_qty",
    "Bal.Qty.": "bal_qty",
}

COLUMN_MAPS = {
    "leather_stock": MRP_COLUMNS,
    "sole_stock": MRP_COLUMNS,
    "sale_orders": SALE_ORDER_COLUMNS,
    "samples": SAMPLE_COLUMNS,
    "io_to_last": IO_COLUMNS,
}

# Columns stored as ISO 'YYYY-MM-DD' text (SQLite date functions work on them).
DATE_COLUMNS = {
    "sale_orders": ["order_date", "ex_factory_date", "csa_date"],
    "samples": ["doc_date", "delivery_date", "status_date", "dispatch_date", "entry_date"],
    "io_to_last": ["order_date", "ex_factory_date", "csa_date"],
}

# Identifier-like columns kept as TEXT even when Excel stores them as numbers.
TEXT_ID_COLUMNS = {
    "sale_orders": ["order_no"],
    "samples": ["doc_no"],
    "io_to_last": ["io_no", "order_no"],
}

# In the MRP report and IO sheet a blank quantity cell means zero.
ZERO_FILL_COLUMNS = {
    "leather_stock": [v for v in MRP_COLUMNS.values() if v not in ("product_name", "uom")],
    "sole_stock": [v for v in MRP_COLUMNS.values() if v not in ("product_name", "uom")],
    "io_to_last": ["io_qty", "taken_qty", "bal_qty"],
}

# ---------------------------------------------------------------------------
# Business descriptions (fed to the LLM next to the live schema)
# ---------------------------------------------------------------------------
_MRP_DOC = {
    "group_name": "Material sub-group the item belongs to (derived from the group header rows of the report)",
    "product_name": "Material / item name (e.g. 'Cow Swede Black 1.4-1.6', 'Tpr Sheet Sole Amaze Dark Brown 46')",
    "uom": "Unit of measure",
    "req_qty": "Required quantity for open orders (MRP requirement)",
    "iss_qty": "Quantity already issued to production",
    "excess_iss": "Quantity issued in excess of requirement",
    "bal_qty": "Balance quantity still to be issued against requirement",
    "cr_stock": "Current stock in hand",
    "short_fall": "Shortfall = MAX(bal_qty - cr_stock, 0)  (stock alone can't cover balance)",
    "excess_stk": "Excess stock = MAX(cr_stock - bal_qty, 0)",
    "excess_amt": "Value (Rs.) of the excess stock",
    "ord_qty": "Quantity on order with suppliers (open purchase orders)",
    "osj_qty": "Quantity out for job-work / outside supply (OSJ) that will come back",
    "excess_qty": "Net excess after pipeline = MAX(cr_stock + ord_qty + osj_qty - bal_qty, 0)",
    "to_be_ord": "Net quantity still TO BE ORDERED = MAX(bal_qty - cr_stock - ord_qty - osj_qty, 0)",
    "min_lvl": "Minimum stock level",
    "tbo_aml": "To-be-ordered considering minimum level (TBO after min level)",
    "age_overdue": "Part of to_be_ord whose requirement date has already passed (< 0 days)",
    "age_0_30": "Part of to_be_ord required within 0-30 days",
    "age_31_60": "Part of to_be_ord required within 31-60 days",
    "age_61_90": "Part of to_be_ord required within 61-90 days",
    "age_91_120": "Part of to_be_ord required within 91-120 days",
    "age_121_150": "Part of to_be_ord required within 121-150 days",
    "age_151_plus": "Part of to_be_ord required after 151+ days",
}

TABLE_DOCS = {
    "leather_stock": (
        "Leather material MRP / stock-position report (one row per leather item). "
        "Group-header and grand-total rows of the Excel were removed; their totals equal SUM() over these rows.",
        _MRP_DOC,
    ),
    "sole_stock": (
        "Sole material MRP / stock-position report (one row per sole item; soles are sized, e.g. '... 46'). "
        "Same columns and formulas as leather_stock.",
        _MRP_DOC,
    ),
    "material_stock": (
        "VIEW = leather_stock UNION ALL sole_stock with an extra column `category` ('Leather' or 'Sole'). "
        "Use it for questions spanning both materials.",
        dict(_MRP_DOC, category="'Leather' or 'Sole'"),
    ),
    "sale_orders": (
        "Open export sale orders (one row per order line). All amounts are quantities (pairs); there is no price column.",
        {
            "s_no": "Serial number in the report",
            "order_no": "Sale order number (TEXT, e.g. '29134'). Joins to io_to_last.order_no",
            "order_date": "Order date (ISO text YYYY-MM-DD)",
            "merchandiser": "Merchandiser handling the order (person)",
            "destination": "Destination port / country (free text, inconsistent spellings)",
            "mode_of_shipment": "Mode of shipment: Sea / Road / Air",
            "terms": "Delivery terms (Fob)",
            "currency": "Order currency (EURO, GBP, USD, INR, 'Rs.')",
            "ex_factory_date": "Ex-factory (shipment due) date, ISO text",
            "order_status": "Order status",
            "product_name": "Article + colour + (buyer), e.g. '35891 Grey/Navy (Shoe Zone)'",
            "article_no": "DERIVED: leading article/style number of product_name (e.g. '35891'); cross-sheet key",
            "buyer": "DERIVED: buyer/brand from the last parentheses of product_name (e.g. 'Shoe Zone', 'M&S', 'Walmart')",
            "uom": "Unit (Pair)",
            "order_qty": "Ordered quantity in pairs",
            "csa_date": "CSA date (customer sample approval date), ISO text; NULL when not yet approved",
            "cur_bk_ref_no": "Current booking / production-plan reference no. (e.g. 'P-4166'); NULL when not booked",
        },
    ),
    "samples": (
        "Sample register (development / confirmation / PPS etc. samples), one row per sample document.",
        {
            "s_no": "Serial number in the report",
            "doc_no": "Sample document number (TEXT)",
            "doc_date": "Sample request date, ISO text",
            "sample_type": "Type of sample (see verified values; note ERP spellings 'Devlopment', 'Slection')",
            "merchandiser": "Merchandiser who requested the sample",
            "delivery_date": "Required delivery date of the sample, ISO text",
            "style_name": "Style description (usually starts with the article number, e.g. '38018 cw sde musk(moss)')",
            "article_no": "DERIVED: leading article number of style_name (NULL if none); cross-sheet key",
            "uom": "Unit (Pair)",
            "sample_qty": "Sample quantity in pairs",
            "product_class": "Product class (Shoe, Pu Sandle, Chappal, ...)",
            "colour": "Colour ('(N.A.)' = not applicable)",
            "shoe_last": "Shoe last (mould) name",
            "leather": "Upper leather used",
            "lining": "Lining material",
            "status": "Sample status: Open / Completed / Cancelled",
            "status_date": "Date the status was last set (completion/cancellation date), ISO text",
            "dispatch_date": "Dispatch date (currently always NULL in the data)",
            "entered_by": "ERP user who entered the row",
            "entry_date": "Entry date in ERP, ISO text",
        },
    ),
    "io_to_last": (
        "Internal Orders (I.O.) pending for cutting / lasting in production - one row per internal order.",
        {
            "io_no": "Internal order number (TEXT)",
            "order_no": "Linked sale order number (TEXT) -> sale_orders.order_no",
            "order_date": "Order date, ISO text",
            "buyer": "Buyer name (short, e.g. 'Walmart', 'Marks & Sp', 'next')",
            "merchandiser": "Merchandiser (mostly NULL / 'n.a.')",
            "ex_factory_date": "Ex-factory date, ISO text",
            "style": "Article + colour + (buyer), same format as sale_orders.product_name",
            "article_no": "DERIVED: leading article number of style; cross-sheet key",
            "colour": "Colour",
            "shoe_last": "Shoe last (mould) name",
            "conveyor": "Production conveyor / line code (C1-C4)",
            "leather": "Upper leather",
            "qtr_lining": "Quarter lining material",
            "grd": "Yes/No readiness flag 'Grd.' (grading done)",
            "trl": "Yes/No readiness flag 'Trl.' (trial done)",
            "m_chk": "Yes/No readiness flag 'M.Chk.' (material check)",
            "c_d": "Yes/No readiness flag 'C.D.' (cutting die ready)",
            "csa_date": "CSA date, ISO text",
            "io_qty": "Internal order quantity (pairs)",
            "taken_qty": "Quantity already taken for cutting/lasting",
            "bal_qty": "Balance pending = io_qty - taken_qty",
        },
    ),
}
