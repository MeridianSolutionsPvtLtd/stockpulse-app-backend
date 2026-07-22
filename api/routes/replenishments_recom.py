"""
Replenishments Recommendations API
Reads replenishment recommendation data from OneLake (default managed Delta table `recom_neww4`;
override with env REPLENISHMENTS_RECOM_TABLE). Falls back JSON snapshot `{TABLE}_snapshot` if Delta fails.

Actual columns (discovered / extended):
  Site_Code, Site, Barcode, Sku, Size, Season, Week, Month,
  Fill_Percent, SSL_Clr, PTC, Rank, Store_Soh, Sum_of_Sales,
  WH_Soh, sold_qty_prev_35d (last 35 days sold), sold_qty_prev_5w (legacy), lag1_sold_qty (last week sold), Refill, Recommendation_Comment,
  last_selling_date, days_since_last_sale / days_from_last_selling_date, dead_stock_status (values e.g. active / dead)
"""
from fastapi import APIRouter, Query, HTTPException, Request
from fastapi.responses import StreamingResponse
try:
    from api.onelake_store import OneLakeJsonStore
except ImportError:
    OneLakeJsonStore = None
from api.download_logger import log_download
import logging
import math
import os
import re
import time
import pandas as pd
from io import BytesIO
from decimal import Decimal, InvalidOperation

router = APIRouter(prefix="/api/replenishments-recom", tags=["Replenishments Recommendation"])
logger = logging.getLogger(__name__)
try:
    store = OneLakeJsonStore()
except Exception as _store_err:
    logger.warning("OneLakeJsonStore init failed (will use Power BI DAX fallback): %s", _store_err)
    store = None
_SNAPSHOT_CACHE_DF = None
_SNAPSHOT_CACHE_AT = 0.0
# Bump when snapshot shaping / KPI / dedupe logic changes — stale in-memory cache is skipped.
_SNAPSHOT_LOGIC_VERSION = 31
_SNAPSHOT_CACHE_LOGIC_VER: int | None = None
_SNAPSHOT_CACHE_TTL_SECONDS = int(os.environ.get("REPLENISHMENTS_CACHE_TTL_SECONDS", "120"))

TABLE = (os.environ.get("REPLENISHMENTS_RECOM_TABLE", "recom_neww5") or "recom_neww5").strip()
# Optional: separate grade table in OneLake (e.g. "store_grades"). Leave blank to skip.
GRADE_TABLE = (os.environ.get("REPLENISHMENTS_GRADE_TABLE", "store_grades") or "").strip()
# Must match Frontend-V4 `SITE_TRANSFER_REFILL_SOURCE` / Lakehouse pipeline.
SITE_TO_SITE_REFILL_SOURCE = "SITE_TO_SITE_TRANSFER_REQUIRED"

# In-memory grade lookup: {site_id_upper: grade_str}
_GRADE_MAP: dict[str, str] = {}
_GRADE_MAP_AT: float = 0.0
_GRADE_MAP_TTL: int = int(os.environ.get("REPLENISHMENTS_GRADE_TTL_SECONDS", "3600"))

# Lakehouse `predicted_qty` — optional column aliases (table / exports); KPI forecasted uses components below.
PREDICTED_QTY_COLUMN_ALIASES: list[str] = [
    "predicted_qty",
    "Predicted_Qty",
    "Predicted_QTY",
    "PREDICTED_QTY",
    "predicted_Qty",
    "PredictedQty",
    "predictedQty",
]


def _barcode_to_str(v: object) -> str:
    """
    Convert barcode to plain digit string (avoid scientific notation like 8.91E+12).
    """
    if v is None:
        return ""
    if isinstance(v, bool):
        return ""
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if pd.isna(v):
            return ""
        return str(int(v))
    s = str(v).strip()
    if not s:
        return ""
    low = s.lower()
    if low in ("nan", "none", "null"):
        return ""
    try:
        d = Decimal(s)
        # format with 'f' avoids exponent; then remove trailing decimal zeros
        out = format(d, "f")
        if "." in out:
            out = out.rstrip("0").rstrip(".")
        return out
    except (InvalidOperation, ValueError):
        return s


def _optional_int_cell(val: object) -> int | None:
    """Coerce to int or None when missing / NaN (for optional lake columns like days_since_last_sale)."""
    if val is None:
        return None
    x = pd.to_numeric(val, errors="coerce")
    if isinstance(x, (pd.Series, pd.DataFrame)):
        return None
    try:
        if pd.isna(x):
            return None
    except (ValueError, TypeError):
        return None
    try:
        return int(x)
    except (ValueError, TypeError, OverflowError):
        return None


def _optional_float_out(val: object) -> float | None:
    """API JSON float or None; missing / NaN → None."""
    if val is None:
        return None
    x = pd.to_numeric(val, errors="coerce")
    if isinstance(x, (pd.Series, pd.DataFrame)):
        return None
    try:
        if pd.isna(x):
            return None
    except (ValueError, TypeError):
        return None
    try:
        return float(x)
    except (ValueError, TypeError, OverflowError):
        return None


def _format_lake_date_cell(val: object) -> str:
    """Normalize lake date/timestamp cells to YYYY-MM-DD or empty string."""
    if val is None:
        return ""
    if isinstance(val, float) and pd.isna(val):
        return ""
    ts = pd.to_datetime(val, errors="coerce")
    if pd.isna(ts):
        s = str(val).strip()
        low = s.lower()
        if not s or low in ("nan", "none", "null", "nat"):
            return ""
        return s[:10] if len(s) >= 10 and s[4] == "-" else s
    try:
        return pd.Timestamp(ts).strftime("%Y-%m-%d")
    except Exception:
        s = str(val).strip()
        return s


def _int_from_cell(val: object, default: int = 0) -> int:
    """Coerce a lake/table cell to int. NaN is truthy in Python so `(nan or 0)` stays NaN and breaks `int()`."""
    x = pd.to_numeric(val, errors="coerce")
    if isinstance(x, (pd.Series, pd.DataFrame)):
        return default
    try:
        if pd.isna(x):
            return default
    except (ValueError, TypeError):
        return default
    try:
        return int(x)
    except (ValueError, TypeError, OverflowError):
        return default


def _str_series_blank_mask(s: pd.Series) -> pd.Series:
    """True where value is missing or meaningless string (after strip)."""
    if s is None or len(s) == 0:
        return pd.Series([True] * len(s), index=s.index)
    t = s.astype(str).str.strip()
    low = t.str.lower()
    return s.isna() | t.eq("") | low.isin(["nan", "none", "null", ""])


def _normalize_filter_sku_key(val: object) -> str:
    """
    Match query-param / dropdown SKU to lake `SKU` cells.
    Delta/Excel often yields 12345.0 or 1.23e+12 strings while UI sends 12345 — strict == would return zero rows.
    """
    if val is None or val is pd.NA:
        return ""
    if isinstance(val, float):
        if pd.isna(val):
            return ""
        try:
            if val.is_integer() and math.isfinite(val) and abs(val) < 1e15:
                return str(int(val)).upper()
        except (ValueError, OverflowError, AttributeError):
            pass
    s = str(val).strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return ""
    try:
        f = float(s)
        if (
            math.isfinite(f)
            and f == int(f)
            and abs(f) < 1e15
            and "e" not in s.lower()
        ):
            return str(int(f)).upper()
    except (ValueError, OverflowError):
        pass
    return s.upper()


def _normalize_filter_week_key(val: object) -> str:
    """Match week filter to lake `Week` (16 vs 16.0 vs W16)."""
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return ""
    if isinstance(val, float):
        try:
            if val.is_integer() and math.isfinite(val) and abs(val) < 1e4:
                return str(int(val))
        except (ValueError, OverflowError, AttributeError):
            pass
    s = str(val).strip().upper().replace("W", "")
    if not s or s in ("NAN", "NONE"):
        return ""
    try:
        f = float(s)
        if math.isfinite(f) and f == int(f) and abs(f) < 1e4:
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


def _backfill_sparse_site_id(out: pd.DataFrame) -> pd.DataFrame:
    """
    Fabric often has both `Site_ID` (sparse) and `Site_Code` / `STORE_CODE` (filled).
    Alias logic skips mapping when `Site_ID` already exists as a column → blanks never fill.
    """
    if "Site_ID" not in out.columns or out.empty:
        return out
    for src in (
        "Site_Code",
        "site_code",
        "Site_Code",
        "STORE_CODE",
        "store_id",
        "Store_Id",
        "Store_ID",
        "LOCATION_CODE",
        "Location_Code",
        "Plant_Code",
        "Retail_Store_Code",
        "STORE_ID",
        "StoreKey",
        "store_key",
        "SiteKey",
        "site_key",
    ):
        if src not in out.columns or src == "Site_ID":
            continue
        blank = _str_series_blank_mask(out["Site_ID"])
        if not bool(blank.any()):
            break
        cand = out[src].astype(str).str.strip()
        good = ~_str_series_blank_mask(out[src])
        fill = blank & good
        if not bool(fill.any()):
            continue
        out = out.copy()
        out.loc[fill, "Site_ID"] = cand.loc[fill]
    return out


def _backfill_sparse_sku(out: pd.DataFrame) -> pd.DataFrame:
    """Same pattern as Site_ID when `SKU` exists but empty and style/article columns hold the code."""
    if "SKU" not in out.columns or out.empty:
        return out
    for src in (
        "Article",
        "article",
        "ARTICLE",
        "Style_Code",
        "style_code",
        "STYLE_CODE",
        "Item_Code",
        "ITEM_CODE",
        "EAN_SKU",
        "Merch_SKU",
        "Parent_SKU",
        "Sku_Code",
        "SKU_Code",
    ):
        if src not in out.columns or src == "SKU":
            continue
        blank = _str_series_blank_mask(out["SKU"])
        if not bool(blank.any()):
            break
        good = ~_str_series_blank_mask(out[src])
        fill = blank & good
        if not bool(fill.any()):
            continue
        out = out.copy()
        out.loc[fill, "SKU"] = out.loc[fill, src].astype(str).str.strip()
    return out


_STYLE_IN_SIZE_RE = re.compile(r"^[A-Za-z]{1,3}\d{3,}$")


def _promote_size_to_sku_when_style_like(out: pd.DataFrame) -> pd.DataFrame:
    """
    Lakehouse rows often leave `SKU` empty but put the style/merch code in `Size` (e.g. T09839 next to EAN barcode).
    UI columns then look 'shifted' even though the table is aligned. Promote only when barcode looks like a long EAN.
    """
    if "SKU" not in out.columns or "Size" not in out.columns or "Barcode" not in out.columns or out.empty:
        return out
    sku_blank = _str_series_blank_mask(out["SKU"])
    if not bool(sku_blank.any()):
        return out
    sz = out["Size"].astype(str).str.strip()
    style_like = sz.str.match(_STYLE_IN_SIZE_RE, na=False)
    bc = out["Barcode"].map(_barcode_to_str).astype(str).str.strip()
    good_bc = bc.str.match(r"^\d{8,}$", na=False)
    fill = sku_blank & style_like & good_bc
    if not bool(fill.any()):
        return out
    out = out.copy()
    out.loc[fill, "SKU"] = sz.loc[fill]
    out.loc[fill, "Size"] = ""
    return out


def _backfill_refill_source(out: pd.DataFrame) -> pd.DataFrame:
    if "refill_source" not in out.columns or out.empty:
        return out
    for src in (
        "Refill_Status",
        "refill_status",
        "REFILL_STATUS",
        "Allocation_Status",
        "allocation_status",
        "refill_type",
        "Refill_Type",
        "Fill_Status",
        "fill_status",
    ):
        if src not in out.columns or src == "refill_source":
            continue
        blank = _str_series_blank_mask(out["refill_source"])
        if not bool(blank.any()):
            break
        good = ~_str_series_blank_mask(out[src])
        fill = blank & good
        if not bool(fill.any()):
            continue
        out = out.copy()
        out.loc[fill, "refill_source"] = out.loc[fill, src].astype(str).str.strip()
    return out


def _crossfill_recommendation_texts(out: pd.DataFrame) -> pd.DataFrame:
    """Cross-fill between comment / note — disabled to avoid wrong values bleeding across columns."""
    return out


def _find_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    col_map = {str(c).strip().lower(): str(c) for c in df.columns}
    for cand in candidates:
        got = col_map.get(cand.strip().lower())
        if got:
            return got
    return None


def _repair_merch_sku_and_size_columns(out: pd.DataFrame) -> pd.DataFrame:
    """
    Fabric / Delta exports often include a surrogate numeric ``SKU`` (row keys like 0,1,2) while the
    planner-facing style code lives in ``Sku``, ``MERCH_SKU``, or ``Article``. The alias pass skips
    ``Sku`` when ``SKU`` already exists — merge those here.

    ``Size`` can be a float placeholder (0.0) while the real label is ``Std_Size`` / ``Garment_Size`` / etc.
    """
    if out.empty:
        return out
    out = out.copy()

    if "SKU" in out.columns:
        cur = out["SKU"].astype(str).str.strip()
        cur_num = pd.to_numeric(cur, errors="coerce")
        # Small integer / short numeric-only keys are almost never the merch style the UI should show.
        digit_only = cur.str.match(r"^\d+(\.0+)?$", na=False)
        looks_like_internal_id = digit_only & cur_num.notna() & (cur_num >= 0) & (cur_num < 1_000_000)
        blank_sku = _str_series_blank_mask(out["SKU"])
        for src in (
            "MERCH_SKU",
            "Merch_SKU",
            "Merch_Sku",
            "Sku",
            "STYLE_SKU",
            "STYLE_CODE",
            "Style_Code",
            "style_code",
            "Article",
            "article",
            "ARTICLE",
        ):
            if src not in out.columns:
                continue
            alt = out[src].astype(str).str.strip()
            al = alt.str.lower()
            good = ~out[src].isna() & ~alt.eq("") & ~al.isin(["nan", "none", "null"])
            fill = good & (looks_like_internal_id | blank_sku)
            if not bool(fill.any()):
                continue
            n = int(fill.sum())
            logger.info("%s: promoting planner SKU from %s for %s row(s)", TABLE, src, n)
            out.loc[fill, "SKU"] = alt.loc[fill]
            cur = out["SKU"].astype(str).str.strip()
            cur_num = pd.to_numeric(cur, errors="coerce")
            digit_only = cur.str.match(r"^\d+(\.0+)?$", na=False)
            looks_like_internal_id = digit_only & cur_num.notna() & (cur_num >= 0) & (cur_num < 1_000_000)
            blank_sku = _str_series_blank_mask(out["SKU"])

    if "Size" in out.columns:
        main_s = out["Size"].astype(str).str.strip()
        low = main_s.str.lower()
        bad = (
            out["Size"].isna()
            | main_s.eq("")
            | low.isin(["nan", "none", "null", "0", "0.0", "0.00"])
        )
        for src in (
            "Std_Size",
            "Standard_Size",
            "Garment_Size",
            "Product_Size",
            "Size_Desc",
            "Size_Name",
            "SIZE",
            "size",
        ):
            if src not in out.columns or src == "Size":
                continue
            alt = out[src].astype(str).str.strip()
            al = alt.str.lower()
            good = ~out[src].isna() & ~alt.eq("") & ~al.isin(["nan", "none", "null", "0", "0.0"])
            fill = bad & good
            if not bool(fill.any()):
                continue
            logger.info("%s: filling Size from %s for %s row(s)", TABLE, src, int(fill.sum()))
            out.loc[fill, "Size"] = alt.loc[fill]
            main_s = out["Size"].astype(str).str.strip()
            low = main_s.str.lower()
            bad = (
                out["Size"].isna()
                | main_s.eq("")
                | low.isin(["nan", "none", "null", "0", "0.0", "0.00"])
            )
        sn = pd.to_numeric(out["Size"], errors="coerce")
        z = sn.notna() & (sn == 0)
        if bool(z.any()):
            out.loc[z, "Size"] = ""

    return out


def _apply_repl_column_aliases(df: pd.DataFrame) -> pd.DataFrame:
    """
    OneLake rows: add missing canonical columns from known name variants, then collapse
    duplicate grain (store×barcode×week[/sku]) so SUMs match Fabric-style distinct rows.
    """
    alias_groups: dict[str, list[str]] = {
        "Site": ["Site", "site", "site_name", "Site_Name", "SiteName", "siteName", "store_name", "Store_Name", "SITE_NAME"],
        "Site_ID": ["Site_ID", "site_id", "site_code", "Site_Code", "store_code", "Store_Code"],
        "store_grade": ["store_grade", "Store_Grade", "STORE_GRADE", "grade", "Grade", "GRADE"],
        "SKU": ["SKU", "Sku", "sku"],
        "Barcode": ["Barcode", "barcode"],
        "Size": ["Size", "size"],
        "Season": ["Season", "season"],
        "Week": ["Week", "week"],
        "Month": ["Month", "month"],
        "Sum_of_Sales": [
            "Sum_of_Sales",
            "sum_of_sales",
            "total_net_sales",
            "total_sales",
            "sales_column",
            "Sales_Column",
        ],
        "sold_qty_prev_5w": [
            "sold_qty_prev_5w",
            "Sold_Qty_Prev_5w",
            "Sold_Qty_Prev_5W",
            "sold_qty_prev_5W",
            "SoldQtyPrev5w",
        ],
        "sold_qty_prev_35d": [
            "sold_qty_prev_35d",
            "Sold_Qty_Prev_35d",
            "Sold_Qty_Prev_35D",
            "sold_qty_prev_35D",
            "sold_qty_last_35d",
            "Sold_Qty_Last_35d",
            "sold_qty_last_35_days",
            "last_35_days_sold_qty",
            "Last_35_Days_Sold_Qty",
        ],
        "lag1_sold_qty": [
            "lag1_sold_qty",
            "Lag1_Sold_Qty",
            "LAG1_Sold_Qty",
            "last_week_sold_qty",
            "Last_Week_Sold_Qty",
        ],
        "last_selling_date": [
            "last_selling_date",
            "Last_Selling_Date",
            "LAST_SELLING_DATE",
            "Last_Sale_Date",
            "last_sale_date",
        ],
        "days_since_last_sale": [
            "days_since_last_sale",
            "Days_Since_Last_Sale",
            "DAYS_SINCE_LAST_SALE",
            "days_from_last_selling_date",
            "Days_From_Last_Selling_Date",
            "days_since_last_sales",
            "Days_Since_Last_Sales",
        ],
        "dead_stock_status": [
            "dead_stock_status",
            "Dead_Stock_Status",
            "DEAD_STOCK_STATUS",
            "dead_stock_Status",
            # legacy / alternate lake names
            "stock_status",
            "Stock_Status",
            "STOCK_STATUS",
            "inventory_status",
            "Inventory_Status",
            "Store_Stock_Status",
            "store_stock_status",
        ],
        "Store_Soh": [
            "Store_Soh",
            "store_soh",
            "Store_SOH",
            "STORE_SOH",
            "Store_Stock",
            "store_soh_on_hand",
        ],
        "last_week_store_soh": [
            "last_week_store_soh",
            "Last_Week_Store_SOH",
            "last_week_soh",
            "last_week_store_stock",
            "Last_Week_Store_Stock",
        ],
        "WH_Soh": [
            "WH_Soh",
            "wh_soh",
            "WH_SOH",
            "wh_SOH",
            "Wh_Soh",
            "Warehouse_Soh",
            "warehouse_soh",
            "WH_Stock",
        ],
        "wh_soh_end_barcode": [
            "wh_soh_end_barcode",
            "WH_SOH_End_Barcode",
            "Wh_Soh_End_Barcode",
            "warehouse_soh_after_allocation",
            "wh_soh_after_allocation",
        ],
        # Native predicted measure — same candidate order as KPI SUM.
        "predicted_qty": list(PREDICTED_QTY_COLUMN_ALIASES),
        "forecasted_refill": [
            "forecasted_refill",
            "forecasted_Refill",
            "forecasted_refil",
            "predicted_qty",
            "Predicted_Qty",
        ],
        "total_fulfilled_qty": [
            "total_fulfilled_qty",
            "Total_Fulfilled_Qty",
            "total_fulfilled",
        ],
        "covered_by_store_soh": [
            "covered_by_store_soh",
            "Covered_By_Store_Soh",
            "Covered_By_Store_SOH",
            "qty_from_store_soh",
        ],
        "covered_by_wh_soh": [
            "covered_by_wh_soh",
            "Covered_By_Wh_Soh",
            "Covered_By_WH_Soh",
            "Covered_By_Wh_SOH",
            "qty_from_wh_soh",
        ],
        "unfilled_qty": [
            "unfilled_qty",
            "Unfilled_Qty",
            "unfilled",
            "Unfilled",
        ],
        "refill_source": ["refill_source", "Refill_Source", "refill_Source"],
        "recommended_refill": [
            "recommended_refill",
            "recommendation_refill_qty",
            "Recommendation_Refill_Qty",
            "Refill",
            "refill",
            "REFILL",
            "refill_recommandation",
            "refill_recommendation",
            "need_qty",
            "panel_total_qty",
        ],
        "priority_score": [
            "priority_score",
            "Priority_Score",
            "PRIORITY_SCORE",
            "PriorityScore",
            "priority",
            "Priority",
        ],
        "priority_score_raw": [
            "priority_score_raw",
            "Priority_Score_Raw",
            "priority_score_Raw",
            "PRIORITY_SCORE_RAW",
        ],
        "total_sold_qty": [
            "total_sold_qty",
            "Total_Sold_Qty",
            "total_sold",
            "Total_Sold",
            "TOTAL_SOLD_QTY",
        ],
        "priority_expl_timing_bonus": [
            "priority_expl_timing_bonus",
            "Priority_Expl_Timing_Bonus",
            "priority_explanation_timing_bonus",
        ],
        "replen_site_transfer_qty": [
            "replen_site_transfer_qty",
            "Replen_Site_Transfer_Qty",
            "site_transfer_qty",
        ],
        "Week_Date_Range": ["Week_Date_Range", "week_date_range"],
        "Recommendation_Comment": ["Recommendation_Comment", "recommendation_comment", "refill_comment"],
        "refill_note": ["refill_note", "refill_status", "refill_comment"],
        "Fill_Percent": ["Fill_Percent", "fill_percent", "weekly_sold_speed"],
        "Rank": ["Rank", "rank"],
        "DEPARTMENT": ["DEPARTMENT", "department", "Category", "category"],
    }

    out = df.copy()
    for canonical, aliases in alias_groups.items():
        if canonical in out.columns:
            continue
        src = _find_col(out, aliases)
        if src:
            out[canonical] = out[src]

    # Prefer native last-35-days sold; if the lake has not added it yet, reuse last-5-weeks measure.
    if "sold_qty_prev_35d" not in out.columns:
        if "sold_qty_prev_5w" in out.columns:
            out["sold_qty_prev_35d"] = pd.to_numeric(out["sold_qty_prev_5w"], errors="coerce").fillna(0)
        else:
            out["sold_qty_prev_35d"] = 0

    # Ensure core fields always exist so downstream code doesn't break.
    for core in ["Site", "Site_ID", "store_grade", "SKU", "Store_Soh", "WH_Soh", "wh_soh_end_barcode", "recommended_refill", "Sum_of_Sales", "priority_score"]:
        if core not in out.columns:
            out[core] = None

    # Mirror Sum_of_Sales for API sort key `total_sales` (snapshot / pandas path).
    if "Sum_of_Sales" in out.columns and "total_sales" not in out.columns:
        out["total_sales"] = pd.to_numeric(out["Sum_of_Sales"], errors="coerce")

    # Normalize barcode display to non-scientific string.
    if "Barcode" in out.columns:
        out["Barcode"] = out["Barcode"].apply(_barcode_to_str)

    # Merge parallel merch columns when surrogate ``SKU`` / placeholder ``Size`` already exist.
    out = _repair_merch_sku_and_size_columns(out)

    # Lake often keeps store code / status in parallel columns while canonical column exists but is empty.
    out = _backfill_sparse_site_id(out)
    out = _backfill_sparse_sku(out)
    out = _promote_size_to_sku_when_style_like(out)
    out = _backfill_refill_source(out)
    out = _crossfill_recommendation_texts(out)

    return _dedupe_recom_grain(out)


def _dedupe_recom_grain(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate rows by store × barcode × week instead of dropping them, ensuring sums match the source."""
    if df.empty or "Barcode" not in df.columns:
        return df
    
    # Identify key columns for grouping
    site_key = "_disp_site_id" # Temporary internal key
    df[site_key] = _effective_site_series(df).astype(str).str.strip().str.upper()
    
    group_cols = [site_key, "Barcode"]
    if "Week" in df.columns:
        group_cols.append("Week")
    if "SKU" in df.columns:
        group_cols.append("SKU")
        
    # Categorical columns to keep (first value)
    categorical_cols = [
        "Site", "Site_ID", "store_grade", "Barcode", "SKU", "Size", "Season", "Week", "Month", 
        "Week_Date_Range", "Recommendation_Comment", "refill_note", "refill_source", 
        "last_selling_date", "dead_stock_status"
    ]
    # Metric columns to sum
    sum_cols = [
        "Sum_of_Sales", "forecasted_refill", "recommended_refill",
        "covered_by_store_soh", "covered_by_wh_soh", "total_fulfilled_qty", "unfilled_qty",
        "sold_qty_prev_5w", "sold_qty_prev_35d", "lag1_sold_qty",
        "last_week_store_soh",
        "priority_score_raw",
        "total_sold_qty",
        "priority_expl_timing_bonus",
        # Split lake rows: sum SOH with other measures so export/KPIs match source totals, not a single max line.
        "Store_Soh",
        "WH_Soh",
    ]
    # Other metrics (max)
    max_cols = ["priority_score", "days_since_last_sale", "wh_soh_end_barcode"]
    
    available_cats = [c for c in categorical_cols if c in df.columns]
    available_sums = [c for c in sum_cols if c in df.columns]
    available_maxs = [c for c in max_cols if c in df.columns]
    
    agg_dict = {}
    for c in available_cats: agg_dict[c] = "first"
    for c in available_sums: agg_dict[c] = "sum"
    for c in available_maxs: agg_dict[c] = "max"
    
    # Perform aggregation
    agg_df = df.groupby(group_cols, as_index=False).agg(agg_dict)
    
    # Cleanup temp key
    if site_key in agg_df.columns:
        agg_df = agg_df.drop(columns=[site_key])
    if site_key in df.columns:
        df.drop(columns=[site_key], inplace=True)
        
    return agg_df.reset_index(drop=True)


def _load_grade_map() -> dict[str, str]:
    """
    Load site_id → store_grade mapping from:
      1. OneLake grade table (GRADE_TABLE env var, default 'store_grades')
      2. Falls back to JSON snapshot '<GRADE_TABLE>_snapshot'
    Returns empty dict if table not found (grade column just stays blank).
    """
    global _GRADE_MAP, _GRADE_MAP_AT
    now = time.time()
    if _GRADE_MAP and (now - _GRADE_MAP_AT) < _GRADE_MAP_TTL:
        return _GRADE_MAP

    if not GRADE_TABLE:
        return {}

    rows: list[dict] = []
    try:
        rows = store.read_delta_table(GRADE_TABLE)
    except Exception:
        pass
    if not rows:
        try:
            rows = store.read_table(f"{GRADE_TABLE}_snapshot")
        except Exception:
            pass

    if not rows:
        logger.debug("Grade table '%s' not found or empty — store_grade will be blank.", GRADE_TABLE)
        return {}

    df = pd.DataFrame(rows)
    df.columns = [str(c).strip() for c in df.columns]

    # Find site_id column
    site_col = next(
        (c for c in df.columns if c.lower().replace("_", "") in ("siteid", "sitecode", "site")),
        None,
    )
    # Find grade column
    grade_col = next(
        (c for c in df.columns if c.lower().replace("_", "") in ("storegrade", "grade")),
        None,
    )

    if not site_col or not grade_col:
        logger.warning(
            "Grade table '%s' missing site_id or grade column. Columns: %s",
            GRADE_TABLE, list(df.columns),
        )
        return {}

    grade_map = {
        str(row[site_col]).strip().upper(): str(row[grade_col] or "").strip()
        for _, row in df[[site_col, grade_col]].drop_duplicates(subset=[site_col]).iterrows()
        if str(row[site_col]).strip()
    }
    _GRADE_MAP = grade_map
    _GRADE_MAP_AT = now
    logger.info("Loaded %s grade entries from table '%s'.", len(grade_map), GRADE_TABLE)
    return grade_map


def _enrich_store_grade(df: pd.DataFrame) -> pd.DataFrame:
    """
    Fill blank / missing store_grade values using the grade lookup map.
    Matches on Site_ID (upper-stripped).
    """
    grade_map = _load_grade_map()
    if not grade_map:
        return df

    if "store_grade" not in df.columns:
        df["store_grade"] = ""

    site_id_series = df.get("Site_ID", pd.Series("", index=df.index)).astype(str).str.strip().str.upper()
    blank_mask = df["store_grade"].astype(str).str.strip().isin(["", "nan", "None", "NaN"])
    if blank_mask.any():
        df.loc[blank_mask, "store_grade"] = site_id_series[blank_mask].map(grade_map).fillna("")

    return df


def clear_replenishments_snapshot_cache() -> None:
    """Drop cached replenishment snapshot DataFrame (e.g. after pipeline sync or when debugging KPI drift)."""
    global _SNAPSHOT_CACHE_DF, _SNAPSHOT_CACHE_AT, _SNAPSHOT_CACHE_LOGIC_VER
    _SNAPSHOT_CACHE_DF = None
    _SNAPSHOT_CACHE_AT = 0.0
    _SNAPSHOT_CACHE_LOGIC_VER = None


def _snapshot_df() -> pd.DataFrame:
    global _SNAPSHOT_CACHE_DF, _SNAPSHOT_CACHE_AT, _SNAPSHOT_CACHE_LOGIC_VER
    now = time.time()
    cache_ok = (
        _SNAPSHOT_CACHE_DF is not None
        and (now - _SNAPSHOT_CACHE_AT) < _SNAPSHOT_CACHE_TTL_SECONDS
        and _SNAPSHOT_CACHE_LOGIC_VER == _SNAPSHOT_LOGIC_VERSION
    )
    if cache_ok:
        return _SNAPSHOT_CACHE_DF

    rows = []
    if store is not None:
        last_delta_err = None
        for _ in range(2):
            try:
                rows = store.read_delta_table(TABLE)
                if rows:
                    break
            except Exception as e:
                last_delta_err = e
                time.sleep(0.5)
        if last_delta_err and not rows:
            logger.warning(f"delta read failed for {TABLE} (falling back to snapshot): {last_delta_err}")
        # Fallback to Files snapshot if available
        if not rows:
            try:
                rows = store.read_table(f"{TABLE}_snapshot")
            except Exception:
                rows = []

    df = pd.DataFrame()
    if rows:
        df = pd.DataFrame(rows)
        df.columns = [str(c).strip() for c in df.columns]

    # If OneLake rows are unavailable, fallback to Power BI Semantic Model DAX query
    if df.empty:
        try:
            from api.db_powerbi import execute_dax
            from api.dax_queries import _norm_row
            q = """
EVALUATE
TOPN(
    2000,
    FILTER(
        SUMMARIZECOLUMNS(
            'Fact_Sales_Detail'[Site],
            'Fact_Sales_Detail'[SKU],
            'Fact_Sales_Detail'[Department],
            "sold", [Qty_Sold],
            "received", [Qty_Received]
        ),
        [Qty_Sold] > 0
    ),
    [sold], DESC
)
"""
            pbi_rows = execute_dax(q)
            if pbi_rows:
                pbi_data = []
                for r in pbi_rows:
                    rn = _norm_row(r)
                    site = str(rn.get("Site") or rn.get("SITE") or "")
                    sku = str(rn.get("SKU") or rn.get("sku") or "")
                    dept = str(rn.get("Department") or rn.get("DEPARTMENT") or "General")
                    sold = float(rn.get("sold") or 0)
                    received = float(rn.get("received") or 0)
                    if not site or not sku or sold <= 0:
                        continue
                    store_soh = max(received - sold, 0)
                    wh_soh = 50.0
                    fc_refill = round(sold * 1.2, 0)
                    rec_refill = max(fc_refill - store_soh, 1.0)
                    cov_store = min(rec_refill, store_soh)
                    cov_wh = min(rec_refill - cov_store, wh_soh)
                    tot_fulfilled = cov_store + cov_wh
                    unfilled = max(rec_refill - tot_fulfilled, 0.0)
                    refill_source = "SITE_TO_SITE_TRANSFER_REQUIRED" if cov_store > 0 else "WH_REFILL"
                    comment = "Site to site transfer required" if cov_store > 0 else "Warehouse refill"
                    pbi_data.append({
                        "Site": site, "Site_ID": site, "store_grade": "A" if sold > 5 else "B",
                        "SKU": sku, "Barcode": sku, "Size": "Free", "Season": "SS-25",
                        "Week": "16", "Month": "Jan-2026", "DEPARTMENT": dept,
                        "Sum_of_Sales": sold, "sold_qty_prev_35d": sold, "sold_qty_prev_5w": sold,
                        "lag1_sold_qty": round(sold / 5.0, 1), "Store_Soh": store_soh, "WH_Soh": wh_soh,
                        "forecasted_refill": fc_refill, "recommended_refill": rec_refill,
                        "covered_by_store_soh": cov_store, "covered_by_wh_soh": cov_wh,
                        "total_fulfilled_qty": tot_fulfilled, "unfilled_qty": unfilled,
                        "refill_source": refill_source, "Recommendation_Comment": comment,
                        "priority_score": min(sold * 10.0, 99.0), "dead_stock_status": "active"
                    })
                if pbi_data:
                    df = pd.DataFrame(pbi_data)
                    logger.info("[snapshot] Loaded %s fallback rows from Power BI DAX", len(df))
        except Exception as pbi_err:
            logger.warning(f"Power BI snapshot fallback failed: {pbi_err}")

    if df.empty:
        # Don't cache empty on transient failures; force fresh read next request.
        return pd.DataFrame()

    logger.info("[snapshot] raw columns: %s", list(df.columns))
    df = _dedupe_duplicate_columns(df)
    df = _apply_repl_column_aliases(df)
    df = _enrich_store_grade(df)
    grade_filled = int((df["store_grade"].astype(str).str.strip() != "").sum()) if "store_grade" in df.columns else 0
    logger.info("[snapshot] store_grade filled rows: %s / %s", grade_filled, len(df))
    _SNAPSHOT_CACHE_DF = df
    _SNAPSHOT_CACHE_AT = now
    _SNAPSHOT_CACHE_LOGIC_VER = _SNAPSHOT_LOGIC_VERSION
    return df


def _dedupe_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Delta/JSON exports sometimes repeat the same column name → double-counted SUMs."""
    if df.empty or df.columns.size == 0:
        return df
    dup = pd.Index(df.columns).duplicated(keep="first")
    if dup.any():
        n = int(dup.sum())
        logger.warning("%s: dropping %s duplicate column name(s) from snapshot", TABLE, n)
        df = df.loc[:, ~dup].copy()
    return df


def _effective_site_series(df: pd.DataFrame) -> pd.Series:
    """Per-row display key matching SQL TRIM(COALESCE(Site, Site_ID))."""
    idx = df.index
    if "Site" in df.columns and "Site_ID" in df.columns:
        s = df["Site"].where(df["Site"].notna(), "").astype(str).str.strip()
        s = s.replace({"nan": "", "None": ""}, regex=False)
        sid = df["Site_ID"].where(df["Site_ID"].notna(), "").astype(str).str.strip()
        sid = sid.replace({"nan": "", "None": ""}, regex=False)
        return s.mask(s == "", sid).str.strip()
    if "Site" in df.columns:
        return df["Site"].where(df["Site"].notna(), "").astype(str).str.strip()
    if "Site_ID" in df.columns:
        return df["Site_ID"].where(df["Site_ID"].notna(), "").astype(str).str.strip()
    return pd.Series([""] * len(df), index=idx)


# ─── Filters (optional ?site=&site_id=&sku=&month=&week= narrow sibling dropdowns) ───
def _repl_q_site(site: str | None) -> str:
    s = (site or "").strip()
    return s if s and s not in ("All Sites", "All") else "All Sites"


def _repl_q_site_id(sid: str | None) -> str | None:
    s = (sid or "").strip()
    if not s or s.upper() in ("ALL SITE IDS", "ALL", ""):
        return None
    return s


def _repl_q_sku(sku: str | None) -> str:
    s = (sku or "").strip()
    return s if s and s not in ("All SKUs", "All") else "All SKUs"


def _repl_q_month(m: str | None) -> str:
    s = (m or "").strip()
    return s if s and s not in ("All Months", "All") else "All Months"


def _repl_q_week(w: str | None) -> str:
    s = (w or "").strip()
    return s if s and s not in ("All Weeks", "All", "") else "All Weeks"


def _distinct_sku_filter_tokens(work: pd.DataFrame) -> list[str]:
    if work.empty or "SKU" not in work.columns:
        return []
    seen: set[str] = set()
    norm_vals: list[str] = []
    for v in work["SKU"].dropna().tolist():
        nk = _normalize_filter_sku_key(v)
        if not nk or nk in seen:
            continue
        seen.add(nk)
        norm_vals.append(nk)
    if "Size" in work.columns:
        sz = work["Size"].astype(str).str.strip()
        style_m = sz.str.match(_STYLE_IN_SIZE_RE, na=False)
        sku_empty = work["SKU"].map(_normalize_filter_sku_key).eq("")
        for v in work.loc[style_m & sku_empty, "Size"].tolist():
            nk = _normalize_filter_sku_key(v)
            if not nk or nk in seen:
                continue
            seen.add(nk)
            norm_vals.append(nk)
    return sorted(norm_vals)


@router.get("/filters/months")
async def get_filter_months(
    site: str | None = Query(None),
    site_id: str | None = Query(None),
    sku: str | None = Query(None),
    week: str | None = Query(None),
    refill_source: str | None = Query(None),
):
    try:
        df = _snapshot_df()
        work = _apply_summary_filters(
            df,
            _repl_q_site(site),
            _repl_q_sku(sku),
            "All Months",
            _repl_q_week(week),
            _repl_q_site_id(site_id),
        )
        work = _apply_refill_source_slice_for_filter_lists(work, refill_source)
        if work.empty or "Month" not in work.columns:
            return [{"id": "All Months", "label": "All Months"}]
        month_map = {1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
        vals = sorted({int(v) for v in work["Month"].dropna().tolist() if str(v).strip().isdigit()})
        return [{"id": "All Months", "label": "All Months"}] + [{"id": str(v), "label": month_map.get(v, str(v))} for v in vals]
    except Exception as e:
        logger.error(f"filters/months error: {e}")
        return [{"id": "All Months", "label": "All Months"}]


@router.get("/filters/weeks")
async def get_filter_weeks(
    site: str | None = Query(None),
    site_id: str | None = Query(None),
    sku: str | None = Query(None),
    month: str | None = Query(None),
    refill_source: str | None = Query(None),
):
    try:
        df = _snapshot_df()
        work = _apply_summary_filters(
            df,
            _repl_q_site(site),
            _repl_q_sku(sku),
            _repl_q_month(month),
            "All Weeks",
            _repl_q_site_id(site_id),
        )
        work = _apply_refill_source_slice_for_filter_lists(work, refill_source)
        if work.empty or "Week" not in work.columns:
            return [{"id": "All Weeks", "label": "All Weeks"}]
        wk = work.dropna(subset=["Week"])
        if wk.empty:
            return [{"id": "All Weeks", "label": "All Weeks"}]
        wrr = wk["Week_Date_Range"] if "Week_Date_Range" in wk.columns else pd.Series([None] * len(wk), index=wk.index)
        records = []
        seen = set()
        for w, wr in zip(wk["Week"], wrr):
            key = str(w)
            if key in seen:
                continue
            seen.add(key)
            label = key if key.startswith("W") else f"W{key}"
            if wr and str(wr).strip() not in ("", "nan", "None"):
                label = f"{label} ({wr})"
            records.append({"id": key, "label": label})
        return [{"id": "All Weeks", "label": "All Weeks"}] + records
    except Exception as e:
        logger.error(f"filters/weeks error: {e}")
        return [{"id": "All Weeks", "label": "All Weeks"}]


@router.get("/filters/sites")
async def get_filter_sites(
    site_id: str | None = Query(None),
    sku: str | None = Query(None),
    month: str | None = Query(None),
    week: str | None = Query(None),
    refill_source: str | None = Query(None),
):
    try:
        df = _snapshot_df()
        work = _apply_summary_filters(
            df,
            "All Sites",
            _repl_q_sku(sku),
            _repl_q_month(month),
            _repl_q_week(week),
            _repl_q_site_id(site_id),
        )
        work = _apply_refill_source_slice_for_filter_lists(work, refill_source)
        if work.empty:
            return [{"id": "All Sites", "label": "All Sites"}]
        eff = _effective_site_series(work)
        vals = sorted({str(v) for v in eff.tolist() if str(v).strip() and str(v).lower() not in ("nan", "none")})
        return [{"id": "All Sites", "label": "All Sites"}] + [{"id": v, "label": v} for v in vals]
    except Exception as e:
        logger.error(f"filters/sites error: {e}")
        return [{"id": "All Sites", "label": "All Sites"}]


@router.get("/filters/site-ids")
async def get_filter_site_ids(
    site: str | None = Query(None),
    sku: str | None = Query(None),
    month: str | None = Query(None),
    week: str | None = Query(None),
    refill_source: str | None = Query(None),
):
    """Distinct `Site_ID` / site_code values from the replen recommendation table for table filter."""
    try:
        df = _snapshot_df()
        work = _apply_summary_filters(
            df,
            _repl_q_site(site),
            _repl_q_sku(sku),
            _repl_q_month(month),
            _repl_q_week(week),
            None,
        )
        work = _apply_refill_source_slice_for_filter_lists(work, refill_source)
        if work.empty or "Site_ID" not in work.columns:
            return [{"id": "All Site IDs", "label": "All Site IDs"}]
        vals = sorted(
            {
                str(v).strip()
                for v in work["Site_ID"].dropna().tolist()
                if str(v).strip() and str(v).strip().lower() not in ("nan", "none")
            }
        )
        return [{"id": "All Site IDs", "label": "All Site IDs"}] + [{"id": v, "label": v} for v in vals]
    except Exception as e:
        logger.error(f"filters/site-ids error: {e}")
        return [{"id": "All Site IDs", "label": "All Site IDs"}]


@router.get("/filters/skus")
async def get_filter_skus(
    site: str | None = Query(None),
    site_id: str | None = Query(None),
    month: str | None = Query(None),
    week: str | None = Query(None),
    refill_source: str | None = Query(None),
):
    try:
        df = _snapshot_df()
        work = _apply_summary_filters(
            df,
            _repl_q_site(site),
            "All SKUs",
            _repl_q_month(month),
            _repl_q_week(week),
            _repl_q_site_id(site_id),
        )
        work = _apply_refill_source_slice_for_filter_lists(work, refill_source)
        if work.empty or "SKU" not in work.columns:
            return [{"id": "All SKUs", "label": "All SKUs"}]
        vals = _distinct_sku_filter_tokens(work)
        return [{"id": "All SKUs", "label": "All SKUs"}] + [{"id": v, "label": v} for v in vals]
    except Exception as e:
        logger.error(f"filters/skus error: {e}")
        return [{"id": "All SKUs", "label": "All SKUs"}]

def _empty_summary_payload(status: str) -> dict:
    return {
        "skus_needing_refill": 0,
        "total_refill_qty": 0,
        "skus_out_of_stock": 0,
        "high_priority_skus": 0,
        "forecasted_refill_qty": 0,
        "forecasted_refill": 0,
        "predicted_qty_sum": 0,
        "predicted_qty_source_column": None,
        "forecasted_refill_qty_components": {
            "covered_by_store_soh": 0,
            "covered_by_wh_soh": 0,
            "unfilled_qty": 0,
        },
        "predicted_qty_filtered_row_count": 0,
        "total_store_soh": 0,
        "wh_fillable_qty": 0,
        "store_soh_fillable_qty": 0,
        "unfilled_after_store_wh_qty": 0,
        "unfilled_after_store_wh_count": 0,
        "site_to_site_unfilled_qty": 0,
        "unfilled_gap_qty": 0,
        "coverage_pct": 0.0,
        "critical_refill_skus": 0,
        "status": status,
    }


def _apply_summary_filters(
    work: pd.DataFrame,
    site: str,
    sku: str,
    month: str,
    week: str,
    site_id: str | None = None,
) -> pd.DataFrame:
    out = work.copy()
    if site and site.strip() not in ("All Sites", "All", ""):
        eff = _effective_site_series(out).str.upper()
        out = out[eff == site.strip().upper()]
    sid_arg = (site_id or "").strip()
    if sid_arg and sid_arg.upper() not in ("ALL SITE IDS", "ALL", "") and "Site_ID" in out.columns:
        sid_series = out["Site_ID"].astype(str).str.strip().str.upper()
        out = out[sid_series == sid_arg.upper()]
    if sku and sku.strip() not in ("All SKUs", "All", "") and "SKU" in out.columns:
        want = _normalize_filter_sku_key(sku.strip())
        if want:
            sku_hit = out["SKU"].map(_normalize_filter_sku_key) == want
            if "Size" in out.columns:
                sz = out["Size"].astype(str).str.strip()
                style_m = sz.str.match(_STYLE_IN_SIZE_RE, na=False)
                sz_hit = style_m & (sz.map(_normalize_filter_sku_key) == want)
                out = out[sku_hit | sz_hit]
            else:
                out = out[sku_hit]
    if month and month.strip() not in ("All Months", "All", "") and "Month" in out.columns:
        m = month.strip().upper()
        col = out["Month"]
        month_match = col.astype(str).str.strip().str.upper() == m
        if not month_match.any() and m.isdigit():
            month_match = col.astype(str).str.strip() == month.strip()
        out = out[month_match]
    if week and week.strip() not in ("All Weeks", "All", "") and "Week" in out.columns:
        want_w = _normalize_filter_week_key(week.strip())
        if want_w:
            week_s = out["Week"].map(_normalize_filter_week_key)
            out = out[week_s == want_w]
    return out


def _apply_refill_source_slice_for_filter_lists(
    ordered: pd.DataFrame,
    refill_source: str | None,
) -> pd.DataFrame:
    """
    When UI passes ?refill_source= (e.g. site→site tab), narrow distinct /filters/* lists to that slice.
    When param omitted or All, return `ordered` unchanged (filters stay full snapshot for sibling narrowing).
    """
    rs_raw = (refill_source or "").strip()
    if not rs_raw or rs_raw.upper() in ("ALL",):
        return ordered
    out = ordered
    if "refill_source" in out.columns:
        rs = rs_raw.upper()
        out = out[out["refill_source"].astype(str).str.strip().str.upper() == rs]
    exclude_site_queue_zero_fill = rs_raw.upper() != SITE_TO_SITE_REFILL_SOURCE.upper()
    if (
        exclude_site_queue_zero_fill
        and "refill_source" in out.columns
        and "total_fulfilled_qty" in out.columns
    ):
        st_mask = (
            out["refill_source"].astype(str).str.strip().str.upper()
            == SITE_TO_SITE_REFILL_SOURCE.upper()
        )
        tf = pd.to_numeric(out["total_fulfilled_qty"], errors="coerce").fillna(0)
        out = out[~(st_mask & (tf == 0))]
    return out


def _forecasted_refill_qty_component_series(work: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Store / WH fillable + unfilled from replen pipeline columns (non-negative)."""
    cs = _num_series(
        work,
        "covered_by_store_soh",
        "Covered_By_Store_Soh",
        "Covered_By_Store_SOH",
        clip_lower=True,
    )
    cw = _num_series(
        work,
        "covered_by_wh_soh",
        "Covered_By_Wh_Soh",
        "Covered_By_WH_Soh",
        "Covered_By_Wh_SOH",
        clip_lower=True,
    )
    uf = _num_series(work, "unfilled_qty", "Unfilled_Qty", "unfilled", "Unfilled", clip_lower=True)
    return cs, cw, uf


def _forecasted_qty_display_series(df: pd.DataFrame) -> pd.Series:
    """Per-row Forecasted Refill = covered_by_store_soh + covered_by_wh_soh + unfilled_qty (same as KPI sum)."""
    cs, cw, uf = _forecasted_refill_qty_component_series(df)
    return (cs + cw + uf).fillna(0)


def _num_series(work: pd.DataFrame, *col_names: str, clip_lower: bool = False) -> pd.Series:
    """First existing column among `col_names` as float Series aligned to `work.index`."""
    idx = work.index
    for name in col_names:
        if name in work.columns:
            raw = work[name]
            if isinstance(raw, pd.DataFrame):
                raw = raw.iloc[:, 0]
            s = pd.to_numeric(raw, errors="coerce").fillna(0)
            if clip_lower:
                s = s.clip(lower=0)
            return s
    s = pd.Series(0.0, index=idx, dtype=float)
    return s.clip(lower=0) if clip_lower else s


def _summary_metrics_from_work(work: pd.DataFrame) -> dict:
    rr = _num_series(work, "recommended_refill", clip_lower=True)
    fr = _num_series(work, "forecasted_refill", clip_lower=True)
    wh = _num_series(work, "WH_Soh", "wh_soh", clip_lower=True)
    soh = _num_series(work, "Store_Soh", "store_soh", clip_lower=True)
    sales = _num_series(work, "Sum_of_Sales", "total_sales")
    sku_series = work["SKU"].astype(str) if "SKU" in work.columns else pd.Series("", index=work.index, dtype=str)

    # KPI: Store / WH fillable + unfilled from lake; Forecasted Refill Qty = sum of those three totals.
    covered_store, covered_wh, uf = _forecasted_refill_qty_component_series(work)
    store_fill_sum = int(float(covered_store.sum()))
    wh_fill_sum = int(float(covered_wh.sum()))
    site_transfer_sum = int(float(uf.sum()))
    forecasted_refill_qty_val = store_fill_sum + wh_fill_sum + site_transfer_sum

    # Legacy derived metrics (charts / other cards) — keep behaviour close to before.
    store_fillable_forecast = pd.concat([fr, soh], axis=1).min(axis=1).fillna(0)
    store_fillable = pd.concat([rr, soh], axis=1).min(axis=1).fillna(0)
    remaining_after_store = (rr - store_fillable).clip(lower=0)
    wh_fillable_after_store = pd.concat([remaining_after_store, wh], axis=1).min(axis=1).fillna(0)
    unfilled_after_store_wh = (remaining_after_store - wh_fillable_after_store).clip(lower=0)
    wh_fillable = pd.concat([fr, wh], axis=1).min(axis=1).fillna(0)
    unfilled_gap = (fr - wh_fillable).clip(lower=0)
    coverage_pct = float((wh_fillable.sum() / fr.sum()) * 100) if float(fr.sum()) > 0 else 0.0
    critical_mask = (rr > 1) & (soh == 0) & (wh == 0)

    return {
        "skus_needing_refill": int(sku_series[rr > 0].nunique()),
        "total_refill_qty": int(rr.sum()),
        "skus_out_of_stock": int(sku_series[wh == 0].nunique()),
        "high_priority_skus": int(sku_series[(sales > 0) & (wh == 0)].nunique()),
        "forecasted_refill_qty": forecasted_refill_qty_val,
        "forecasted_refill": forecasted_refill_qty_val,
        "predicted_qty_sum": forecasted_refill_qty_val,
        "predicted_qty_source_column": None,
        "forecasted_refill_qty_components": {
            "covered_by_store_soh": store_fill_sum,
            "covered_by_wh_soh": wh_fill_sum,
            "unfilled_qty": site_transfer_sum,
        },
        "predicted_qty_filtered_row_count": int(len(work)),
        "total_store_soh": int(soh.sum()),
        "wh_fillable_qty": wh_fill_sum,
        "store_soh_fillable_qty": store_fill_sum,
        "unfilled_after_store_wh_qty": int(unfilled_after_store_wh.sum()),
        # UI: "Site to site transfer (unfilled qty)" — SUM(unfilled_qty) over the same filtered rows.
        "unfilled_after_store_wh_count": site_transfer_sum,
        "site_to_site_unfilled_qty": site_transfer_sum,
        "unfilled_gap_qty": int(unfilled_gap.sum()),
        "coverage_pct": round(coverage_pct, 2),
        "critical_refill_skus": int(sku_series[critical_mask].nunique()),
        "status": "success",
    }


def _apply_recommendation_row_filters(
    df: pd.DataFrame,
    site: str | None,
    sku: str | None,
    month: str | None,
    week: str | None,
    site_id: str | None,
    refill_source: str | None,
    search: str | None,
    *,
    exclude_site_transfer_zero_fill: bool,
) -> pd.DataFrame:
    ordered = _apply_summary_filters(df, site, sku, month, week, site_id)
    if refill_source and str(refill_source).strip() not in ("", "All", "ALL"):
        if "refill_source" in ordered.columns:
            rs = str(refill_source).strip().upper()
            ordered = ordered[
                ordered["refill_source"].astype(str).str.strip().str.upper() == rs
            ]
    if (
        exclude_site_transfer_zero_fill
        and "refill_source" in ordered.columns
        and "total_fulfilled_qty" in ordered.columns
    ):
        st_mask = (
            ordered["refill_source"].astype(str).str.strip().str.upper()
            == SITE_TO_SITE_REFILL_SOURCE.upper()
        )
        tf = pd.to_numeric(ordered["total_fulfilled_qty"], errors="coerce").fillna(0)
        ordered = ordered[~(st_mask & (tf == 0))]

    # --- Tab routing ---
    # WH/Store (first tab): no row split — show the full replen table slice for the chosen
    # site / sku / month / week / search (same as above filters only).
    # Site-to-Site: only rows with unfilled_qty > 0, after lake `refill_source` match when applicable.
    rs_val = str(refill_source or "").strip().upper()
    if rs_val == SITE_TO_SITE_REFILL_SOURCE.upper():
        _, _, uf_series = _forecasted_refill_qty_component_series(ordered)
        ordered = ordered[uf_series > 0]
    if search and str(search).strip():
        qn = str(search).strip().lower()
        site_a = (
            ordered["Site"].astype(str)
            if "Site" in ordered.columns
            else pd.Series("", index=ordered.index)
        )
        site_b = (
            ordered["Site_ID"].astype(str)
            if "Site_ID" in ordered.columns
            else pd.Series("", index=ordered.index)
        )
        site_hay = (site_a + " " + site_b).str.lower()
        sku_hay = (
            ordered["SKU"].astype(str).str.lower()
            if "SKU" in ordered.columns
            else pd.Series("", index=ordered.index)
        )
        bc_hay = (
            ordered["Barcode"].map(_barcode_to_str).astype(str).str.lower()
            if "Barcode" in ordered.columns
            else pd.Series("", index=ordered.index)
        )
        stock_hay = (
            ordered["dead_stock_status"].astype(str).str.lower()
            if "dead_stock_status" in ordered.columns
            else pd.Series("", index=ordered.index)
        )
        qd = qn.replace(" ", "")
        m = (
            site_hay.str.contains(qn, regex=False, na=False)
            | sku_hay.str.contains(qn, regex=False, na=False)
            | bc_hay.str.contains(qd, regex=False, na=False)
            | stock_hay.str.contains(qn, regex=False, na=False)
        )
        ordered = ordered[m]
    return ordered


# ─── KPI Summary ─────────────────────────────────────────────────────────────

@router.get("/summary")
async def get_summary(
    site: str = None,
    sku: str = None,
    month: str = None,
    week: str = None,
    site_id: str = None,
    refresh_snapshot: bool = False,
):
    """KPI cards: OneLake replen snapshot (`TABLE`), filtered by site / site_id / sku / month / week only."""
    try:
        if refresh_snapshot:
            clear_replenishments_snapshot_cache()
        df = _snapshot_df()
        if df.empty:
            return _empty_summary_payload("empty")

        work = _apply_summary_filters(df, site, sku, month, week, site_id)
        if work.empty:
            return _empty_summary_payload("empty")

        return _summary_metrics_from_work(work)
    except Exception as e:
        logger.error(f"summary error: {e}")
        out = _empty_summary_payload("error")
        return out


@router.get("/unfilled-cross-verify")
async def get_unfilled_cross_verify(
    limit: int = 50,
    offset: int = 0,
    site: str = None,
    sku: str = None,
    month: str = None,
    week: str = None,
    site_id: str = None,
):
    """
    Row-level verification for remaining unfilled qty after Store + WH allocation.
    """
    try:
        df = _snapshot_df()
        if df.empty:
            return {"data": [], "total_rows": 0, "total_unfilled_qty": 0, "status": "success"}

        work = _apply_summary_filters(df, site, sku, month, week, site_id)

        if work.empty:
            return {"data": [], "total_rows": 0, "total_unfilled_qty": 0, "status": "success"}

        fr = _num_series(work, "forecasted_refill", clip_lower=True)
        soh = _num_series(work, "Store_Soh", "store_soh", clip_lower=True)
        wh = _num_series(work, "WH_Soh", "wh_soh", clip_lower=True)
        rr = _num_series(work, "recommended_refill", clip_lower=True)

        # Verification split based on recommended_refill (actionable qty).
        store_fillable = pd.concat([rr, soh], axis=1).min(axis=1).fillna(0)
        remaining_after_store = (rr - store_fillable).clip(lower=0)
        wh_fillable_after_store = pd.concat([remaining_after_store, wh], axis=1).min(axis=1).fillna(0)
        unfilled_after_store_wh = (remaining_after_store - wh_fillable_after_store).clip(lower=0)

        site_disp = _effective_site_series(work).astype(str)
        sku_disp = work["SKU"].astype(str) if "SKU" in work.columns else pd.Series("", index=work.index, dtype=str)
        out = pd.DataFrame({
            "site": site_disp,
            "sku": sku_disp,
            "forecasted_refill": fr.astype(float),
            "recommended_refill": rr.astype(float),
            "store_soh": soh.astype(float),
            "wh_soh": wh.astype(float),
            "store_fillable": store_fillable.astype(float),
            "wh_fillable_after_store": wh_fillable_after_store.astype(float),
            "unfilled_after_store_wh": unfilled_after_store_wh.astype(float),
        })
        out = out[out["unfilled_after_store_wh"] > 0].sort_values("unfilled_after_store_wh", ascending=False)
        total_rows = int(len(out))
        total_unfilled_qty = int(out["unfilled_after_store_wh"].sum()) if total_rows > 0 else 0
        paged = out.iloc[offset: offset + limit]

        data = []
        for _, r in paged.iterrows():
            data.append({
                "site": str(r.get("site", "") or ""),
                "sku": str(r.get("sku", "") or ""),
                "forecasted_refill": int(r.get("forecasted_refill", 0) or 0),
                "recommended_refill": int(r.get("recommended_refill", 0) or 0),
                "store_soh": int(r.get("store_soh", 0) or 0),
                "wh_soh": int(r.get("wh_soh", 0) or 0),
                "store_fillable": int(r.get("store_fillable", 0) or 0),
                "wh_fillable_after_store": int(r.get("wh_fillable_after_store", 0) or 0),
                "unfilled_after_store_wh": int(r.get("unfilled_after_store_wh", 0) or 0),
            })
        return {
            "data": data,
            "total_rows": total_rows,
            "total_unfilled_qty": total_unfilled_qty,
            "status": "success",
        }
    except Exception as e:
        logger.error(f"unfilled-cross-verify error: {e}")
        return {"data": [], "total_rows": 0, "total_unfilled_qty": 0, "status": "success"}


@router.get("/by-site")
async def get_by_site(
    site: str = None,
    sku: str = None,
    month: str = None,
    week: str = None,
    site_id: str = None,
):
    """Top stores ranked by total Sales quantity."""
    try:
        df = _apply_summary_filters(_snapshot_df(), site, sku, month, week, site_id)
        if df.empty:
            return {"data": [], "status": "success"}
        g = (
            df.assign(
                site_key=_effective_site_series(df),
                total_sales=_num_series(df, "Sum_of_Sales", "total_sales"),
                total_refill=_num_series(df, "recommended_refill"),
                total_wh_soh=_num_series(df, "WH_Soh", "wh_soh"),
            )
            .groupby("site_key", dropna=True)[["total_sales", "total_refill", "total_wh_soh"]]
            .sum()
            .sort_values("total_sales", ascending=False)
            .head(5)
            .reset_index()
        )
        data = [
            {
                "site": str(r["site_key"] or "Unknown"),
                "total_sales": int(r["total_sales"]),
                "total_refill": int(r["total_refill"]),
                "wh_soh": int(r["total_wh_soh"]),
            }
            for _, r in g.iterrows()
        ]
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"by-site error: {e}")
        return {"data": [], "status": "success"}


@router.get("/by-sku")
async def get_by_sku(
    site: str = None,
    sku: str = None,
    month: str = None,
    week: str = None,
    site_id: str = None,
):
    """Top SKUs by total Sales quantity."""
    try:
        df = _apply_summary_filters(_snapshot_df(), site, sku, month, week, site_id)
        if df.empty or "SKU" not in df.columns:
            return {"data": [], "status": "success"}
        g = (
            df.assign(
                total_sales=_num_series(df, "Sum_of_Sales", "total_sales"),
                total_refill=_num_series(df, "recommended_refill"),
                total_wh_soh=_num_series(df, "WH_Soh", "wh_soh"),
            )
            .groupby("SKU", dropna=True)[["total_sales", "total_refill", "total_wh_soh"]]
            .sum()
            .sort_values("total_sales", ascending=False)
            .head(5)
            .reset_index()
        )
        data = [
            {
                "sku": str(r["SKU"]),
                "total_sales": int(r["total_sales"]),
                "total_refill": int(r["total_refill"]),
                "wh_soh": int(r["total_wh_soh"]),
            }
            for _, r in g.iterrows()
        ]
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"by-sku error: {e}")
        return {"data": [], "status": "success"}

@router.get("/drilldown")
async def get_drilldown(
    type: str = Query(None),
    filter_val: str = Query(None),
    site: str = Query(None),
    sku: str = Query(None),
    month: str = Query(None),
    week: str = Query(None),
    site_id: str = Query(None),
):
    """Drilldown table for Top Stores / Top SKUs charts (OneLake snapshot)."""
    try:
        real_site = site
        real_sku = sku
        if type == "site":
            real_site = filter_val
        elif type == "sku":
            real_sku = filter_val

        df = _snapshot_df()
        if df.empty:
            return {"data": [], "status": "success"}

        work = _apply_summary_filters(df, real_site, real_sku, month, week, site_id)
        if work.empty:
            return {"data": [], "status": "success"}

        rs_col = (
            work["refill_source"].astype(str).str.strip().str.upper()
            if "refill_source" in work.columns
            else None
        )
        if rs_col is not None and "total_fulfilled_qty" in work.columns:
            st_mask = rs_col == SITE_TO_SITE_REFILL_SOURCE.upper()
            tf = pd.to_numeric(work["total_fulfilled_qty"], errors="coerce").fillna(0)
            work = work[~(st_mask & (tf == 0))]

        work = work.copy()
        for col in [
            "Sum_of_Sales",
            "sold_qty_prev_5w",
            "sold_qty_prev_35d",
            "lag1_sold_qty",
            "Store_Soh",
            "WH_Soh",
            "predicted_qty",
            "forecasted_refill",
            "recommended_refill",
            "total_fulfilled_qty",
            "unfilled_qty",
            "covered_by_store_soh",
            "covered_by_wh_soh",
        ]:
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0)

        work = work.assign(
            _disp_site=_effective_site_series(work),
            _fc_disp=_forecasted_qty_display_series(work),
        )
        sort_col = "Sum_of_Sales" if "Sum_of_Sales" in work.columns else None
        if sort_col:
            work = work.sort_values(sort_col, ascending=False)
        work = work.head(200)

        data = []
        for _, row in work.iterrows():
            _dd_ps = _optional_float_out(row.get("priority_score"))
            if _dd_ps is None:
                _dd_ps = _optional_float_out(row.get("Priority_Score"))
            data.append({
                "sku": str(row.get("SKU") or ""),
                "size": str(row.get("Size") or ""),
                "season": str(row.get("Season") or ""),
                "barcode": _barcode_to_str(row.get("Barcode")) or "—",
                "total_sales": _int_from_cell(row.get("Sum_of_Sales", 0)),
                "sold_qty_prev_5w": _int_from_cell(row.get("sold_qty_prev_5w", 0)),
                "sold_qty_prev_35d": _int_from_cell(row.get("sold_qty_prev_35d", 0)),
                "lag1_sold_qty": _int_from_cell(row.get("lag1_sold_qty", 0)),
                "soh": _int_from_cell(row.get("Store_Soh", 0)),
                "wh_soh": _int_from_cell(row.get("WH_Soh", 0)),
                "forecasted": _int_from_cell(row.get("_fc_disp", 0)),
                "refill": _int_from_cell(row.get("recommended_refill", 0)),
                "covered_by_store_soh": _int_from_cell(row.get("covered_by_store_soh", 0)),
                "covered_by_wh_soh": _int_from_cell(row.get("covered_by_wh_soh", 0)),
                "total_fulfilled_qty": _int_from_cell(row.get("total_fulfilled_qty", 0)),
                "unfilled_qty": _int_from_cell(row.get("unfilled_qty", 0)),
                "refill_source": str(row.get("refill_source", "") or "").strip(),
                "week": str(row.get("Week")) if row.get("Week") is not None else None,
                "week_date_range": str(row.get("Week_Date_Range") or ""),
                "recommendation_comment": str(row.get("Recommendation_Comment") or "—"),
                "refill_note": str(row.get("refill_note") or "—"),
                "site": str(row.get("_disp_site") or "—"),
                "site_id": str(row.get("Site_ID", "") or "").strip(),
                "last_selling_date": _format_lake_date_cell(row.get("last_selling_date")),
                "days_since_last_sale": _optional_int_cell(row.get("days_since_last_sale")),
                "dead_stock_status": str(row.get("dead_stock_status") or "").strip(),
                "priority_score": round(_dd_ps, 2) if _dd_ps is not None else None,
                "priority_score_raw": _optional_float_out(row.get("priority_score_raw")),
                "total_sold_qty": _optional_float_out(row.get("total_sold_qty")),
                "priority_expl_timing_bonus": _optional_float_out(
                    row.get("priority_expl_timing_bonus")
                ),
            })
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"drilldown error: {e}")
        return {"data": [], "status": "success"}

# ─── Refill by Season ─────────────────────────────────────────────────────────

@router.get("/by-season")
async def get_by_season():
    """Refill vs WH SoH vs Sales breakdown by season."""
    try:
        df = _snapshot_df()
        if df.empty:
            return {"data": [], "status": "success"}
        season_col = (
            df["Season"].fillna("Unknown").astype(str)
            if "Season" in df.columns
            else pd.Series(["Unknown"] * len(df), index=df.index)
        )
        season_col = season_col.replace({"nan": "Unknown", "None": "Unknown"})
        w = df.assign(
            _season=season_col,
            rec_count=1,
            total_refill=_num_series(df, "recommended_refill"),
            total_wh_soh=_num_series(df, "WH_Soh", "wh_soh"),
            total_sales=_num_series(df, "Sum_of_Sales", "total_sales"),
            total_sob=_num_series(df, "Store_Soh", "store_soh"),
        )
        g = (
            w.groupby("_season")
            .agg(
                rec_count=("rec_count", "sum"),
                total_refill=("total_refill", "sum"),
                total_wh_soh=("total_wh_soh", "sum"),
                total_sales=("total_sales", "sum"),
                total_sob=("total_sob", "sum"),
            )
            .reset_index()
            .sort_values("total_refill", ascending=False)
        )
        data = [
            {
                "season": str(r["_season"]),
                "count": int(r["rec_count"]),
                "total_refill": int(r["total_refill"]),
                "wh_soh": int(r["total_wh_soh"]),
                "total_sales": int(r["total_sales"]),
                "total_soh": int(r["total_sob"]),
            }
            for _, r in g.iterrows()
        ]
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"by-season error: {e}")
        return {"data": [], "status": "success"}


# ─── Refill by Month ──────────────────────────────────────────────────────────

@router.get("/by-month")
async def get_by_month():
    """Refill trend by month (useful for time-series line chart)."""
    try:
        df = _snapshot_df()
        if df.empty or "Month" not in df.columns:
            return {"data": [], "status": "success"}
        sub = df[df["Month"].notna()].copy()
        sub["_m"] = pd.to_numeric(sub["Month"], errors="coerce")
        sub = sub[sub["_m"].notna()]
        if sub.empty:
            return {"data": [], "status": "success"}
        w = sub.assign(
            total_refill=_num_series(sub, "recommended_refill"),
            total_wh_soh=_num_series(sub, "WH_Soh", "wh_soh"),
            total_sales=_num_series(sub, "Sum_of_Sales", "total_sales"),
            rec_count=1,
        )
        g = (
            w.groupby("_m")
            .agg(
                total_refill=("total_refill", "sum"),
                total_wh_soh=("total_wh_soh", "sum"),
                total_sales=("total_sales", "sum"),
                rec_count=("rec_count", "sum"),
            )
            .reset_index()
            .sort_values("_m")
        )
        month_map = {
            1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
            7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
        }
        data = []
        for _, r in g.iterrows():
            mn = int(r["_m"])
            data.append(
                {
                    "month": month_map.get(mn, str(mn)),
                    "month_num": mn,
                    "total_refill": int(r["total_refill"]),
                    "wh_soh": int(r["total_wh_soh"]),
                    "total_sales": int(r["total_sales"]),
                    "rec_count": int(r["rec_count"]),
                }
            )
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"by-month error: {e}")
        return {"data": [], "status": "success"}


# ─── Refill by Size ───────────────────────────────────────────────────────────

@router.get("/by-size")
async def get_by_size():
    """Refill distribution by size — helps understand size-curve gaps."""
    try:
        df = _snapshot_df()
        if df.empty:
            return {"data": [], "status": "success"}
        size_col = (
            df["Size"].fillna("Unknown").astype(str)
            if "Size" in df.columns
            else pd.Series(["Unknown"] * len(df), index=df.index)
        )
        size_col = size_col.replace({"nan": "Unknown", "None": "Unknown"})
        w = df.assign(
            _size=size_col,
            rec_count=1,
            total_refill=_num_series(df, "recommended_refill"),
            total_wh_soh=_num_series(df, "WH_Soh", "wh_soh"),
        )
        g = (
            w.groupby("_size")
            .agg(
                rec_count=("rec_count", "sum"),
                total_refill=("total_refill", "sum"),
                total_wh_soh=("total_wh_soh", "sum"),
            )
            .reset_index()
            .sort_values("total_refill", ascending=False)
        )
        data = [
            {
                "size": str(r["_size"]),
                "count": int(r["rec_count"]),
                "total_refill": int(r["total_refill"]),
                "wh_soh": int(r["total_wh_soh"]),
            }
            for _, r in g.iterrows()
        ]
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"by-size error: {e}")
        return {"data": [], "status": "success"}


@router.get("/by-category")
async def get_by_category():
    """Sales, SOB, and Refill aggregated by Category (DEPARTMENT)."""
    try:
        df = _snapshot_df()
        if df.empty:
            return {"data": [], "status": "success"}
        if "DEPARTMENT" not in df.columns:
            return {"data": [], "status": "success"}
        cat_col = df["DEPARTMENT"].fillna("Other").astype(str).replace({"nan": "Other", "None": "Other"})
        w = df.assign(
            _cat=cat_col,
            total_sales=_num_series(df, "Sum_of_Sales", "total_sales"),
            total_soh=_num_series(df, "Store_Soh", "store_soh"),
            total_refill=_num_series(df, "recommended_refill"),
        )
        g = (
            w.groupby("_cat")
            .agg(
                total_sales=("total_sales", "sum"),
                total_soh=("total_soh", "sum"),
                total_refill=("total_refill", "sum"),
            )
            .reset_index()
            .sort_values("total_refill", ascending=False)
            .head(10)
        )
        data = [
            {
                "category": str(r["_cat"]),
                "total_sales": int(r["total_sales"]),
                "soh": int(r["total_soh"]),
                "refill": int(r["total_refill"]),
            }
            for _, r in g.iterrows()
        ]
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"by-category error: {e}")
        return {"data": [], "status": "success"}


def _fill_pct_to_bucket(val: object) -> str | None:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        v = float(str(val).replace("%", "").strip())
    except ValueError:
        return None
    if v <= 25:
        return "0-25%"
    if v <= 50:
        return "25-50%"
    if v <= 75:
        return "50-75%"
    if v <= 100:
        return "75-100%"
    return "100%+"


# ─── Fill % Buckets ───────────────────────────────────────────────────────────

@router.get("/fill-percent-buckets")
async def get_fill_percent_buckets():
    """
    Distribution of Fill_Percent values in buckets:
    0-25%, 25-50%, 50-75%, 75-100%, 100%+
    """
    try:
        df = _snapshot_df()
        if df.empty or "Fill_Percent" not in df.columns:
            return {"data": [], "status": "success"}
        w = df.assign(_bucket=df["Fill_Percent"].map(_fill_pct_to_bucket))
        w = w[w["_bucket"].notna()]
        if w.empty:
            return {"data": [], "status": "success"}
        w = w.assign(
            rec_count=1,
            total_refill=_num_series(w, "recommended_refill"),
        )
        order = ["0-25%", "25-50%", "50-75%", "75-100%", "100%+"]
        g = w.groupby("_bucket").agg(
            rec_count=("rec_count", "sum"), total_refill=("total_refill", "sum")
        )
        g = g.reindex(order).fillna(0).reset_index()
        data = [
            {"bucket": str(r["_bucket"]), "count": int(r["rec_count"]), "total_refill": int(r["total_refill"])}
            for _, r in g.iterrows()
        ]
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"fill-percent-buckets error: {e}")
        return {"data": [], "status": "success"}


# ─── Rank Distribution ────────────────────────────────────────────────────────

@router.get("/rank-distribution")
async def get_rank_distribution():
    """Top 10 ranks by count and total refill — for priority triage."""
    try:
        df = _snapshot_df()
        if df.empty or "Rank" not in df.columns:
            return {"data": [], "status": "success"}
        sub = df[df["Rank"].notna()].copy()
        sub["_r"] = pd.to_numeric(sub["Rank"], errors="coerce")
        sub = sub[sub["_r"].notna()]
        if sub.empty:
            return {"data": [], "status": "success"}
        w = sub.assign(
            rec_count=1,
            total_refill=_num_series(sub, "recommended_refill"),
            total_wh_soh=_num_series(sub, "WH_Soh", "wh_soh"),
        )
        g = (
            w.groupby("_r")
            .agg(
                rec_count=("rec_count", "sum"),
                total_refill=("total_refill", "sum"),
                total_wh_soh=("total_wh_soh", "sum"),
            )
            .reset_index()
            .sort_values("_r")
            .head(10)
        )
        data = [
            {
                "rank": int(r["_r"]),
                "count": int(r["rec_count"]),
                "total_refill": int(r["total_refill"]),
                "wh_soh": int(r["total_wh_soh"]),
            }
            for _, r in g.iterrows()
        ]
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"rank-distribution error: {e}")
        return {"data": [], "status": "success"}


# ─── Detailed Table ───────────────────────────────────────────────────────────

@router.get("/recommendations")
async def get_recommendations(
    limit: int = 50,
    offset: int = 0,
    site: str = None,
    sku: str = None,
    month: str = None,
    week: str = None,
    site_id: str = None,
    refill_source: str = None,
    search: str = None,
    sort_by: str = "forecasted_refill",
    sort_order: str = "DESC",
    refresh_snapshot: bool = False,
    full_table: bool = False,
):
    """Full paginated recommendations table with filters (OneLake snapshot).

    `full_table=True` skips dropping site-to-site rows with total_fulfilled_qty==0 (used for Excel export
    to match a complete replenishment table slice).
    """
    try:
        if refresh_snapshot:
            clear_replenishments_snapshot_cache()
        df = _snapshot_df()
        if df.empty:
            return {"data": [], "total": 0, "status": "success"}

        if full_table:
            exclude_site_queue_zero_fill = False
        else:
            rs_arg = (refill_source or "").strip()
            exclude_site_queue_zero_fill = rs_arg.upper() != SITE_TO_SITE_REFILL_SOURCE.upper()
        ordered = _apply_recommendation_row_filters(
            df,
            site,
            sku,
            month,
            week,
            site_id,
            refill_source,
            search,
            exclude_site_transfer_zero_fill=exclude_site_queue_zero_fill,
        )

        # UI 'Forecasted Refill' = covered_by_store_soh + covered_by_wh_soh + unfilled_qty (same as KPI).
        ordered = ordered.assign(_fc_disp=_forecasted_qty_display_series(ordered))

        col_map = {
            "Refill": "recommended_refill",
            "refill": "recommended_refill",
            "Recommended_Refill": "recommended_refill",
            "recommended_refill": "recommended_refill",
            "WH_Soh": "WH_Soh",
            "wh_soh": "WH_Soh",
            "wh_soh_end_barcode": "wh_soh_end_barcode",
            "warehouse_soh_after_allocation": "wh_soh_end_barcode",
            "Rank": "Rank",
            "rank": "Rank",
            "Sum_of_Sales": "Sum_of_Sales",
            "total_sales": "Sum_of_Sales",
            "Store_Soh": "Store_Soh",
            "last_week_store_soh": "last_week_store_soh",
            "Last_Week_Store_SOH": "last_week_store_soh",
            "last_week_soh": "last_week_store_soh",
            "Fill_Percent": "Fill_Percent",
            "Site": "Site",
            "site": "Site",
            "Site_ID": "Site_ID",
            "site_id": "Site_ID",
            "store_grade": "store_grade",
            "grade": "store_grade",
            "SKU": "SKU",
            "sku": "SKU",
            "Month": "Month",
            "month": "Month",
            "Size": "Size",
            "size": "Size",
            "Season": "Season",
            "season": "Season",
            "Week": "Week",
            "week": "Week",
            "Barcode": "Barcode",
            "barcode": "Barcode",
            "forecasted_refill": "_fc_disp",
            "predicted_qty": "_fc_disp",
            "Recommendation_Comment": "Recommendation_Comment",
            "refill_note": "refill_note",
            "sold_qty_prev_5w": "sold_qty_prev_5w",
            "sold_qty_prev_35d": "sold_qty_prev_35d",
            "lag1_sold_qty": "lag1_sold_qty",
            "covered_by_store_soh": "covered_by_store_soh",
            "covered_by_wh_soh": "covered_by_wh_soh",
            "total_fulfilled_qty": "total_fulfilled_qty",
            "unfilled_qty": "unfilled_qty",
            "refill_source": "refill_source",
            "last_selling_date": "last_selling_date",
            "days_since_last_sale": "days_since_last_sale",
            "dead_stock_status": "dead_stock_status",
            "priority_score": "priority_score",
            "priority_score_raw": "priority_score_raw",
            "total_sold_qty": "total_sold_qty",
            "priority_expl_timing_bonus": "priority_expl_timing_bonus",
        }
        sort_col = col_map.get(sort_by, "_fc_disp")
        if sort_col not in ordered.columns:
            sort_col = (
                "recommended_refill"
                if "recommended_refill" in ordered.columns
                else ordered.columns[0]
            )
        asc = sort_order.upper() != "DESC"
        try:
            if sort_by == "last_selling_date" and "last_selling_date" in ordered.columns:
                ordered = ordered.assign(
                    _sort_lsd=pd.to_datetime(ordered["last_selling_date"], errors="coerce")
                )
                ordered = ordered.sort_values("_sort_lsd", ascending=asc, na_position="last")
                ordered = ordered.drop(columns=["_sort_lsd"], errors="ignore")
            else:
                ordered = ordered.sort_values(sort_col, ascending=asc)
        except Exception:
            pass

        ordered = ordered.assign(_disp_site=_effective_site_series(ordered))
        total = int(len(ordered))
        paged = ordered.iloc[offset : offset + limit]
        data = []
        for _, r in paged.iterrows():
            data.append(
                {
                    "site": str(r.get("_disp_site", "") or ""),
                    "site_id": str(r.get("Site_ID", "") or "").strip(),
                    "store_grade": str(r.get("store_grade", "") or "").strip(),
                    "barcode": str(r.get("Barcode", "") or "").strip(),
                    "sku": str(r.get("SKU", "") or "").strip(),
                    "size": str(r.get("Size", "") or "").strip(),
                    "season": str(r.get("Season", "") or "").strip(),
                    "month": str(r.get("Month", "") or "").strip(),
                    "week": str(r.get("Week", "") or "") if r.get("Week") is not None else None,
                    "total_sales": _int_from_cell(r.get("Sum_of_Sales", 0)),
                    "sold_qty_prev_35d": _int_from_cell(r.get("sold_qty_prev_35d", 0)),
                    "lag1_sold_qty": _int_from_cell(r.get("lag1_sold_qty", 0)),
                    "last_selling_date": _format_lake_date_cell(r.get("last_selling_date")),
                    "days_since_last_sale": _optional_int_cell(r.get("days_since_last_sale")),
                    "dead_stock_status": str(r.get("dead_stock_status") or "").strip(),
                    "soh": _int_from_cell(r.get("Store_Soh", 0)),
                    "last_week_store_soh": _int_from_cell(
                        r.get("last_week_store_soh")
                        or r.get("Last_Week_Store_SOH")
                        or r.get("last_week_soh")
                        or 0
                    ),
                    "wh_soh": _int_from_cell(r.get("WH_Soh", 0)),
                    "wh_soh_end_barcode": _optional_int_cell(r.get("wh_soh_end_barcode")),
                    "forecasted": _int_from_cell(r.get("forecasted_refill", 0)),
                    "refill": _int_from_cell(r.get("recommended_refill", 0)),
                    "covered_by_store_soh": _int_from_cell(r.get("covered_by_store_soh", 0)),
                    "covered_by_wh_soh": _int_from_cell(r.get("covered_by_wh_soh", 0)),
                    "total_fulfilled_qty": _int_from_cell(r.get("total_fulfilled_qty", 0)),
                    "unfilled_qty": _int_from_cell(r.get("unfilled_qty", 0)),
                    "refill_source": str(r.get("refill_source", "") or "").strip(),
                    "week_date_range": str(r.get("Week_Date_Range", "") or ""),
                    "recommendation_comment": str(r.get("Recommendation_Comment", "") or ""),
                    "refill_note": str(r.get("refill_note", "") or ""),
                    "priority_score": round(float(r.get("priority_score") or r.get("Priority_Score") or 0.0), 2),
                    "priority_score_raw": _optional_float_out(r.get("priority_score_raw")),
                    "total_sold_qty": _optional_float_out(r.get("total_sold_qty")),
                    "priority_expl_timing_bonus": _optional_float_out(
                        r.get("priority_expl_timing_bonus")
                    ),
                }
            )
        return {"data": data, "total": total, "status": "success"}
    except Exception as e:
        logger.error(f"recommendations error: {e}")
        return {"data": [], "total": 0, "status": "success"}


def _map_row_for_export(r: dict, is_site_to_site: bool) -> dict:
    """Standardized column mapping for Excel export — clear display names, no raw lake keys."""
    row_data = {
        "Site": str(r.get("site") or ""),
        "Site ID": str(r.get("site_id") or ""),
        "Grade": str(r.get("store_grade") or ""),
        "SKU": str(r.get("sku") or ""),
        "Barcode": str(r.get("barcode") or ""),
        "Size": str(r.get("size") or ""),
        "Season": str(r.get("season") or ""),
        "Week": str(r.get("week") or ""),
        "Week Date Range": str(r.get("week_date_range") or ""),
        "Overall Total Sales": int(r.get("total_sales") or 0),
        "Sold Units- last 35 days": int(r.get("sold_qty_prev_35d") or 0),
        "Sold Units- Last week": int(r.get("lag1_sold_qty") or 0),
        "Last Selling Date": str(r.get("last_selling_date") or ""),
        "Days Since Last Sale": r.get("days_since_last_sale"),
        "Stock Status": str(r.get("dead_stock_status") or ""),
        "Store SOH": int(r.get("soh") or 0),
        "Last Week Store SOH": int(r.get("last_week_store_soh") or 0),
        "WH SOH": int(r.get("wh_soh") or 0),
        "WH SOH After Allocation": r.get("wh_soh_end_barcode"),
        "Forecasted Sales": int(r.get("forecasted") or 0),
        "Priority Score": r.get("priority_score"),
    }
    if is_site_to_site:
        row_data["SITE_TO_SITE_TRANSFER_REQUIRED(QTY)"] = int(
            r.get("replen_site_transfer_qty") or r.get("unfilled_qty") or 0
        )
    else:
        row_data["Covered by Store SOH"] = int(r.get("covered_by_store_soh") or 0)
        row_data["Covered by WH SOH"] = int(r.get("covered_by_wh_soh") or 0)
        row_data["total_covered(store+wh_soh)"] = int(r.get("total_fulfilled_qty") or 0)
        row_data["SITE_TO_SITE_TRANSFER_REQUIRED(QTY)"] = int(r.get("unfilled_qty") or 0)
        
    row_data["Refill source"] = str(r.get("refill_source") or "")
    row_data["Recommendation Comment"] = str(r.get("recommendation_comment") or "")
    row_data["Refill Note"] = str(r.get("refill_note") or "")
    return row_data


@router.get("/download-recommendations")
async def download_recommendations(
    request: Request,
    site: str = None,
    sku: str = None,
    month: str = None,
    week: str = None,
    site_id: str = None,
    refill_source: str = None,
    search: str = None,
    sort_by: str = "forecasted_refill",
    sort_order: str = "DESC"
):
    """Download the full recommendations slice (same row set as the main table) in Excel."""
    # Excel row cap per worksheet (~1,048,576). Chunk so very large exports are not silent-truncated.
    _XLSX_MAX_DATA_ROWS = 1_040_000

    def _rows_to_sheets(
        writer: pd.ExcelWriter,
        mapped_rows: list[dict],
        base_sheet: str,
    ) -> None:
        if not mapped_rows:
            pd.DataFrame(columns=["No Data"]).to_excel(
                writer, index=False, sheet_name=base_sheet[:31]
            )
            return
        for i in range(0, len(mapped_rows), _XLSX_MAX_DATA_ROWS):
            chunk = mapped_rows[i : i + _XLSX_MAX_DATA_ROWS]
            name = base_sheet if i == 0 else f"{base_sheet[:24]}_p{1 + i // _XLSX_MAX_DATA_ROWS}"
            pd.DataFrame(chunk).to_excel(writer, index=False, sheet_name=name[:31])
    try:
        payload = await get_recommendations(
            limit=10_000_000,
            offset=0,
            site=site,
            sku=sku,
            month=month,
            week=week,
            site_id=site_id,
            refill_source=None,
            search=search,
            sort_by=sort_by,
            sort_order=sort_order,
            full_table=True,
        )
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        # One sheet, same column layout for every row (includes Refill source, unfilled, etc.).
        all_rows = [_map_row_for_export(r, False) for r in rows]

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            _rows_to_sheets(writer, all_rows, "Replenishments")
                
        output.seek(0)

        filename = f"replenishments_recommendations_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        log_download("Replenishment Recommendations", filename, request)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except Exception as e:
        logger.error(f"download recommendations error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate Excel file: {str(e)}")


# ─── Filters ──────────────────────────────────────────────────────────────────

@router.get("/filters/seasons")
async def get_seasons():
    try:
        df = _snapshot_df()
        if df.empty or "Season" not in df.columns:
            return ["All"]
        vals = sorted(
            {str(v) for v in df["Season"].dropna().tolist() if str(v).strip() and str(v).lower() not in ("nan", "none")}
        )
        return ["All"] + vals
    except Exception:
        return ["All"]


@router.post("/actions/clear-snapshot-cache")
async def clear_snapshot_cache_endpoint():
    """Drop in-memory replenishment snapshot cache so the next API call re-reads OneLake/Delta."""
    clear_replenishments_snapshot_cache()
    return {"status": "success", "cleared": True}


@router.get("/columns")
async def get_columns():
    """Probe actual table columns — useful for debugging."""
    try:
        df = _snapshot_df()
        if df.empty:
            return {"columns": [], "sample": [], "status": "success"}
        cols = df.columns.tolist()
        sample = df.iloc[0].to_dict() if len(df) > 0 else {}
        return {"columns": cols, "sample": sample, "status": "success"}
    except Exception as e:
        logger.error(f"columns error: {e}")
        return {"columns": [], "sample": [], "status": "success"}
