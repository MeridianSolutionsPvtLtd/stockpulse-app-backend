import logging
import os
import re
from typing import Any, Dict, List, Tuple, Optional
from io import BytesIO
import math
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
import pandas as pd

from api.onelake_store import OneLakeJsonStore
from api.download_logger import log_download

router = APIRouter(prefix="/api/transfers-recom", tags=["Transfers Recommendation"])
logger = logging.getLogger(__name__)
store = OneLakeJsonStore()
_ZERO_SOH_CACHE: List[Dict[str, Any]] = []
_ZERO_SOH_CACHE_AT = 0.0
_ZERO_SOH_CACHE_SOURCE_TABLE: str | None = None
_ZERO_SOH_CACHE_TTL_SECONDS = 180
_TRANSFER_TABLE_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_TRANSFER_TABLE_CACHE_AT: Dict[str, float] = {}
_TRANSFER_TABLE_CACHE_TTL_SECONDS = 120
# OneLake Delta table used for transfer sync (potential donors/receivers), sku-soh, and lake enrich.
# Point to a newer export by setting env TRANSFER_SYNC_SOURCE_TABLE (e.g. same schema as recom_neww3 + 5w sales columns).
TRANSFER_SOURCE_TABLE = (os.environ.get("TRANSFER_SYNC_SOURCE_TABLE", "recom_neww3") or "recom_neww3").strip()
# Optional: only classify donor/receiver from rows with this `refill_source` (e.g. SITE_TO_SITE_TRANSFER_REQUIRED
# matches Replenishments “Site → site” tab / `REPLEN_SITE_TRANSFER_REFILL_SOURCE` in Frontend-V4).
TRANSFER_SYNC_REFILL_SOURCE = (os.environ.get("TRANSFER_SYNC_REFILL_SOURCE", "") or "").strip()
# OneLake / Delta name for the AI recommended transfer lines (stack schema: recommended_transfers_new5).
RECOMMENDED_TRANSFERS_TABLE = (
    os.environ.get("RECOMMENDED_TRANSFERS_TABLE", "recommended_transfers_new5") or "recommended_transfers_new5"
).strip()
# No Sales Donors tab + /zero-soh/export — managed Delta dbo.<name> (override with env ZERO_SOH_TABLE).
ZERO_SOH_TABLE = (os.environ.get("ZERO_SOH_TABLE", "0_sales_soh_new2") or "0_sales_soh_new2").strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        f = float(value)
        if math.isnan(f) or math.isinf(f):
            return default
        try:
            if pd.isna(f):
                return default
        except Exception:
            pass
        return int(f)
    except Exception:
        return default


def _safe_qty(value: Any, default: int = 0) -> int:
    """Treat negative quantities as 0 for stock math."""
    return max(_safe_int(value, default), 0)


def _norm_transfer_sku_filter(val: Any) -> str:
    """Align transfer filter SKU with row sku (12345.0 vs 12345)."""
    s = str(val or "").strip()
    if not s or s.lower() in ("nan", "none", "null"):
        return ""
    try:
        f = float(s)
        if math.isfinite(f) and f == int(f) and abs(f) < 1e15 and "e" not in s.lower():
            return str(int(f)).upper()
    except (ValueError, OverflowError):
        pass
    return s.upper()


def _norm_transfer_week_token(val: Any) -> str:
    s = str(val or "").strip().upper().replace("W", "")
    if not s or s in ("NAN", "NONE"):
        return ""
    try:
        f = float(s)
        if math.isfinite(f) and f == int(f) and abs(f) < 1e4:
            return str(int(f))
    except (ValueError, OverflowError):
        pass
    return s


def _soft_norm_site_label(s: str) -> str:
    """Uppercase, unify unicode dashes, normalize spaces around hyphens (PT- X vs PT - X)."""
    t = str(s or "")
    for ch in ("\u2013", "\u2014", "\u2212", "–", "—"):
        t = t.replace(ch, "-")
    t = t.upper()
    t = re.sub(r"\s*-\s*", " - ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _recv_site_filter_matches(want_raw: str | None, r: Dict[str, Any]) -> bool:
    """
    Replenishment header often sends display site name; transfer rows carry code + name.
    Match soft-normalized label, substring on long names, or store codes (P492). No fuzzy token-% overlap (that pulled wrong sites).
    """
    if want_raw is None:
        return True
    s = str(want_raw).strip()
    if not s or s in ("All Sites", "All Stores", "All"):
        return True
    want_sn = _soft_norm_site_label(s)
    candidates = [
        str(r.get("receiver_store") or "").strip(),
        str(r.get("receiver_site") or "").strip(),
        str(r.get("receiver_store_name") or "").strip(),
        str(r.get("receiver_site_id") or "").strip(),
    ]
    for c in candidates:
        if not c:
            continue
        cn = _soft_norm_site_label(c)
        if cn == want_sn or (want_sn and cn and (want_sn in cn or cn in want_sn)):
            return True
    name = str(r.get("receiver_store_name") or r.get("receiver_site") or "").strip()
    if name and len(want_sn) >= 6:
        nu = _soft_norm_site_label(name)
        if want_sn in nu or nu in want_sn:
            return True
    want_u = s.upper()
    for tok in re.findall(r"[A-Z0-9]{2,12}", want_u):
        for c in candidates:
            if c and tok == c.upper():
                return True
    return False


def _recv_site_id_filter_matches(want_raw: str | None, r: Dict[str, Any]) -> bool:
    """Site ID filter: exact match, or same numeric tail as P492 vs 492."""
    if want_raw is None:
        return True
    s = str(want_raw).strip()
    if not s or s.upper() in ("ALL SITE IDS", "ALL"):
        return True
    ws = s.upper()
    id_cands = [
        str(r.get("receiver_site_id") or "").strip(),
        str(r.get("receiver_store") or "").strip(),
    ]

    def _strip_p_num(s: str) -> str:
        s = s.upper().strip()
        if len(s) >= 2 and s[0] == "P" and s[1:].isdigit():
            return s[1:].lstrip("0") or s[1:]
        return s.lstrip("0")

    for c in id_cands:
        if not c:
            continue
        if c.upper() == ws:
            return True
    w2 = _strip_p_num(ws)
    for c in id_cands:
        if not c:
            continue
        c2 = _strip_p_num(c)
        if w2 and c2 and w2 == c2:
            return True
    return False


def _transfer_row_matches_recv_filters(
    r: Dict[str, Any],
    *,
    recv_site: str | None,
    recv_site_id: str | None,
    recv_sku: str | None,
    recv_month: str | None,
    recv_week: str | None,
) -> bool:
    if recv_sku and recv_sku not in ("All SKUs", "All"):
        if _norm_transfer_sku_filter(r.get("sku")) != _norm_transfer_sku_filter(recv_sku):
            return False
    if not _recv_site_filter_matches(recv_site, r):
        return False
    if not _recv_site_id_filter_matches(recv_site_id, r):
        return False
    if recv_week and recv_week not in ("All Weeks", "All", ""):
        want_w = _norm_transfer_week_token(recv_week)
        rw = str(
            r.get("receiver_week")
            or r.get("week")
            or r.get("Week")
            or r.get("receiver_lake_week")
            or ""
        ).strip()
        if rw and _norm_transfer_week_token(rw) != want_w:
            return False
    if recv_month and recv_month not in ("All Months", "All", ""):
        want_raw = str(recv_month).strip()
        rm = str(
            r.get("receiver_month")
            or r.get("month")
            or r.get("Month")
            or r.get("receiver_lake_month")
            or ""
        ).strip()
        if rm:
            if rm.upper() == want_raw.upper() or rm == want_raw:
                pass
            else:
                try:
                    if int(float(rm)) != int(float(want_raw)):
                        return False
                except Exception:
                    return False
    return True


def _pick_int(row: Dict[str, Any], *keys: str, default: int = 0) -> int:
    """Pick first available numeric-like field from possible aliases."""
    for k in keys:
        if k in row and row.get(k) not in (None, ""):
            return _safe_int(row.get(k), default)
    return default


def _lake_unfilled_qty_or_none(row: Dict[str, Any]) -> int | None:
    """
    Read **only** the canonical lake unfilled column (default `unfilled_qty`).
    We do not scan other names: columns like `qty_unfilled` / `sum_last4_*` were matching first and
    often holding placeholder `1` for every row, which broke the Transfers table.

    Override column name with env `TRANSFER_LAKE_UNFILLED_COLUMN` (lowercase, after `_normalize_row`).
    """
    key = (os.environ.get("TRANSFER_LAKE_UNFILLED_COLUMN", "") or "unfilled_qty").strip().lower() or "unfilled_qty"
    if key not in row:
        return None
    v = row.get(key)
    if v is None or (isinstance(v, str) and not str(v).strip()):
        return None
    return max(_safe_int(v), 0)


def _pick_sold_qty_last_35d(row: Dict[str, Any]) -> int:
    """
    Units sold in the last 35 days from lake columns (row keys are lower-cased by _normalize_row).
    Includes `sold_qty_prev_5w` from recom_neww3 / replenishments (~5 weeks).
    Falls back to generic `last_35_days_sales` only when no unit column matched (may be amount, not units).
    """
    v = _pick_int(
        row,
        "last_35day_sold_qty",
        "sold_qty_prev_5w",
        "sold_qty_prev_5w_units",
        "sold_qty_last_35d",
        "sold_qty_last_35_days",
        "last_35_days_sold_qty",
        "qty_sold_last_35d",
        "qty_sold_last_35_days",
        "sold_units_last_35d",
        "sold_units_35d",
        "last35d_sold_qty",
        "last_35d_sold_qty",
        "sold_qty_prev_35d",
        default=0,
    )
    if v != 0:
        return v
    return _pick_int(row, "last_35_days_sales", default=0)


def _pick_last_week_sold_qty(row: Dict[str, Any]) -> int:
    """Units sold in the most recent week (lag1_sold_qty)."""
    return _pick_int(
        row,
        "lag1_sold_qty",
        "lag1_sold",
        "last_week_sold_qty",
        "last_week_sales",
        default=0,
    )


def _pick_net_sales_last_35d(row: Dict[str, Any]) -> int:
    """Net sales / revenue in last 35 days when the pipeline exposes a dedicated amount column."""
    return _pick_int(
        row,
        "net_sales_last_35d",
        "last_35_days_net_sales",
        "net_sales_last_35_days",
        "sales_amount_last_35d",
        "revenue_last_35d",
        "total_net_sales_last_35d",
        # recom_neww3 / replenishments lake (often weekly grain; best available amount on the row)
        "sum_of_sales",
        "total_net_sales",
        "total_sales",
        default=0,
    )


def _days_since_transfer(row: Dict[str, Any], fallback_days: int = 0) -> int:
    """
    Prefer precomputed days_since_last_transfer, otherwise compute from last_transfer_date.
    """
    direct = _pick_int(row, "days_since_last_transfer", "Days_Since_Last_Transfer", default=-1)
    # Keep positive direct values; 0 often means missing/default in source.
    if direct > 0:
        return direct

    raw_date = (
        row.get("last_transfer_date")
        or row.get("Last_Transfer_Date")
        or row.get("last_transfer_dt")
        or row.get("Last_Transfer_Dt")
        or row.get("last_purchase_date")
        or row.get("Last_Purchase_Date")
        or row.get("last_purchase_dt")
        or row.get("Last_Purchase_Dt")
    )
    if raw_date in (None, ""):
        return max(_safe_int(fallback_days), 0)
    text = str(raw_date).strip()
    if text.lower() in ("nan", "none", "null", "nat"):
        return max(_safe_int(fallback_days), 0)

    parsed = None
    # Common explicit formats first.
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            break
        except Exception:
            continue
    if parsed is None:
        try:
            parsed = pd.to_datetime(text, errors="coerce", dayfirst=True).to_pydatetime()
        except Exception:
            parsed = None
    if parsed is None or str(parsed) == "NaT":
        return max(_safe_int(fallback_days), 0)
    return max((datetime.now() - parsed).days, 0)


def _row_refill_for_potential(r: Dict[str, Any]) -> int:
    """
    Refill / need qty on potential_donors / potential_receivers rows from lake exports.
    Prefers `qty_still_needed` (transfer snapshot), then unfilled / recommendation aliases.
    """
    return _pick_int(
        r,
        "qty_still_needed",
        "recommendation_refill",
        "unfilled_qty",
        "recommendation_refill_qty",
        "recommended_refill",
        "need_qty",
        default=0,
    )


def _row_warehouse_soh(r: Dict[str, Any]) -> int:
    return _safe_qty(_pick_int(r, "warehouse_soh", "wh_soh", default=0))


def _row_days_last_selling(r: Dict[str, Any]) -> int:
    return _pick_int(
        r,
        "days_from_last_sales",
        "days_from_last_selling_date",
        "days_since_last_sale",
        "days_since_last_sales",
        default=0,
    )


def _row_days_last_transfer(r: Dict[str, Any]) -> int:
    return _pick_int(
        r,
        "days_from_last_transfer",
        "days_from_last_transfer_date",
        "days_since_last_transfer",
        default=0,
    )


def _row_priority_score_value(r: Dict[str, Any]) -> float | None:
    v = r.get("priority_score")
    if v in (None, ""):
        v = r.get("Priority_Score")
    if v in (None, ""):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, 4)


def _row_classification_ok(r: Dict[str, Any], want: str) -> bool:
    """If `classification` is set (e.g. Fabric split), keep only matching rows; else allow all."""
    v = str(r.get("classification") or "").strip().lower()
    if not v:
        return True
    if want == "receiver":
        return v in ("receiver", "recv", "receivers", "potential_receiver", "potential_receivers")
    if want == "donor":
        return v in ("donor", "doner", "donors", "potential_donor", "potential_donors")
    return True


def _site_value(row: Dict[str, Any]) -> str:
    return str(row.get("site") or row.get("site_id") or "")


def _site_label(row: Dict[str, Any]) -> str:
    return str(row.get("site_name") or row.get("site") or row.get("site_id") or "")


def _site_name_index(rows: List[Dict[str, Any]]) -> Dict[str, str]:
    """
    Build a lookup so recommendation rows can always resolve site_id/code to site_name.
    """
    idx: Dict[str, str] = {}
    for r in rows:
        site_id = str(r.get("site_id") or r.get("site") or "").strip()
        site_name = str(r.get("site_name") or "").strip()
        if not site_id or not site_name:
            continue
        idx[site_id] = site_name
        idx[site_id.lower()] = site_name
    return idx


def _receiver_refill_index(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], int]:
    """
    Map (site_key, sku_key, size) -> recommendation_refill.
    Keys are uppercased site + normalized SKU so transfer rows match receiver / replen lookups.
    """
    idx: Dict[Tuple[str, str, str], int] = {}
    for r in rows:
        size = str(r.get("size") or "").strip().upper()
        refill = _row_refill_for_potential(r)
        sku_raw = str(r.get("sku") or "").strip()
        sku_n = _norm_transfer_sku_filter(r.get("sku")) or sku_raw.upper()
        site_keys: List[str] = []
        for k in ("site", "site_id", "site_name"):
            v = str(r.get(k) or "").strip()
            if v and v not in site_keys:
                site_keys.append(v)
        if not site_keys or not (sku_raw or sku_n):
            continue
        sku_variants = list({x for x in (sku_raw, sku_n) if x})
        for site in site_keys:
            su = site.upper()
            for sku_k in sku_variants:
                sk = _norm_transfer_sku_filter(sku_k) or str(sku_k).strip().upper()
                if not sk:
                    continue
                k3 = (su, sk, size)
                k2 = (su, sk, "")
                idx[k3] = max(refill, idx.get(k3, 0))
                idx[k2] = max(refill, idx.get(k2, 0))
    return idx


def _lookup_receiver_refill_from_index(idx: Dict[Tuple[str, str, str], int], rec: Dict[str, Any]) -> int:
    """Resolve recommendation_refill for a transfer row using receiver site / sku variants."""
    sku_raw = str(rec.get("sku") or "").strip()
    sku_n = _norm_transfer_sku_filter(rec.get("sku")) or sku_raw.upper()
    size = str(rec.get("size") or "").strip().upper()
    sites: List[str] = []
    for k in ("receiver_store", "receiver_site_id", "receiver_site", "receiver_store_name"):
        v = str(rec.get(k) or "").strip()
        if v and v not in sites:
            sites.append(v)
    best = 0
    for rs in sites:
        su = rs.upper()
        for sk in {x for x in (sku_raw, sku_n) if x}:
            skn = _norm_transfer_sku_filter(sk) or str(sk).strip().upper()
            if not skn:
                continue
            best = max(best, _safe_int(idx.get((su, skn, size))))
            best = max(best, _safe_int(idx.get((su, skn, ""))))
    return best


def _replen_receiver_lake_index(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """
    (site_upper, sku_norm_upper, size_upper) -> OneLake receiver row facts for Replenishments-style columns.
    """
    idx: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in rows or []:
        sku_u = _norm_transfer_sku_filter(row.get("sku"))
        if not sku_u:
            continue
        size_u = str(row.get("size") or "").strip().upper()
        ts = _pick_int(row, "total_sales", "sum_of_sales", "sales")
        s5 = _pick_int(row, "sold_qty_prev_5w")
        l1 = _pick_int(row, "lag1_sold_qty", "last_week_sold_qty")
        week = str(row.get("week") or "").strip()
        wdr = str(row.get("week_date_range") or "").strip()
        lake_refill = _pick_int(
            row,
            "recommended_refill",
            "recommendation_refill",
            "refill",
            "forecasted_refill",
            "forecasted",
            "refill_recommandation",
            "refill_recommendation",
        )
        soh = _safe_qty(row.get("store_soh"))
        wh = _safe_qty(row.get("wh_soh"))
        season = str(row.get("season") or "").strip()
        bc = str(row.get("barcode") or "").strip()
        mon = str(row.get("month") or "").strip()
        fact: Dict[str, Any] = {
            "receiver_lake_total_sales": ts,
            "receiver_lake_sold_qty_prev_5w": s5,
            "receiver_lake_lag1_sold_qty": l1,
            "receiver_lake_week": week,
            "receiver_lake_week_date_range": wdr,
            "receiver_lake_refill": lake_refill,
            "receiver_lake_soh": soh,
            "receiver_lake_wh_soh": wh,
            "receiver_lake_season": season,
            "receiver_lake_barcode": bc,
            "receiver_lake_month": mon,
        }
        site_keys: set = set()
        for k in ("site_id", "site", "site_name"):
            v = str(row.get(k) or "").strip()
            if v:
                site_keys.add(v.upper())
        for su in site_keys:
            for sz_key in (size_u, ""):
                kt = (su, sku_u, sz_key)
                prev = idx.get(kt)
                if prev is None or ts >= _safe_int(prev.get("receiver_lake_total_sales", 0)):
                    idx[kt] = {**fact}
    return idx


def _enrich_rec_from_receiver_lake(rec: Dict[str, Any], lake_idx: Dict[Tuple[str, str, str], Dict[str, Any]]) -> None:
    if not lake_idx:
        return
    sku_u = _norm_transfer_sku_filter(rec.get("sku"))
    if not sku_u:
        return
    size = str(rec.get("size") or "").strip().upper()
    sites: List[str] = []
    for k in ("receiver_store", "receiver_site_id", "receiver_site", "receiver_store_name"):
        v = str(rec.get(k) or "").strip()
        if v and v not in sites:
            sites.append(v.upper())
    fact = None
    for su in sites:
        fact = lake_idx.get((su, sku_u, size)) or lake_idx.get((su, sku_u, ""))
        if fact:
            break
    if not fact:
        return
    for kk, vv in fact.items():
        rec[kk] = vv


def _movement_units_for_rec(rec: Dict[str, Any]) -> int:
    """Stable transfer qty for UI when total_units is missing / NaN-serialized."""
    tq = _safe_int(rec.get("transfer_qty"))
    if tq <= 0:
        tq = _safe_int(rec.get("transfer_qty_total"))
    if tq > 0:
        return tq
    tu = _safe_int(rec.get("total_units"))
    if tu > 0:
        return tu
    req = _safe_int(rec.get("required_size_units"))
    add = _safe_int(rec.get("additional_size_units"))
    if req + add > 0:
        return req + add
    refill = _safe_int(rec.get("receiver_recommended_refill"))
    rsoh = _safe_qty(rec.get("receiver_soh"))
    if rec.get("receiver_refill_is_net_unfilled") is True:
        need = max(refill, 0)
    else:
        need = max(refill - rsoh, 0)
    if need > 0:
        return max(need, 1)
    if req > 0:
        return req
    du = _safe_qty(rec.get("donor_store_soh") or rec.get("donor_soh"))
    if du > 0 and refill > 0:
        return min(max(refill, 1), max(du, 1))
    return max(tu, 1) if tu > 0 else 1


def _normalize_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k).lower(): v for k, v in row.items()}


def _dedupe_keys_case_insensitive_prefer_lower(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parquet / Delta: schema rejects duplicate field names that differ only by case
    (e.g. Transfer_ID and transfer_id). Keep a single key per logical name; prefer all-lowercase.
    """
    groups: Dict[str, List[Tuple[str, Any]]] = {}
    for k, v in row.items():
        lk = str(k).lower()
        groups.setdefault(lk, []).append((k, v))
    out: Dict[str, Any] = {}
    for pairs in groups.values():
        if len(pairs) == 1:
            k0, v0 = pairs[0]
            out[k0] = v0
            continue
        lower_keys = [p for p in pairs if p[0] == str(p[0]).lower()]
        if lower_keys:
            k0, v0 = lower_keys[0]
            out[k0] = v0
        else:
            k0, v0 = pairs[0]
            out[k0] = v0
    return out


def _first_str_from_row(row: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        low = s.lower()
        if s and low not in ("nan", "none", "null", "nat", ""):
            return s
    return ""


def _row_state(row: Dict[str, Any]) -> str:
    return _first_str_from_row(
        row,
        "state",
        "site_state",
        "store_state",
        "statename",
        "state_name",
        "region",
        "province",
        "st",
        "cust_state",
        "ship_state",
        "billing_state",
        "statecode",
        "state_code",
    )


def _row_city(row: Dict[str, Any]) -> str:
    return _first_str_from_row(
        row,
        "city",
        "site_city",
        "store_city",
        "city_name",
        "location",
        "town",
        "cust_city",
        "ship_city",
        "billing_city",
    )


def _row_store_type(row: Dict[str, Any]) -> str:
    return _first_str_from_row(
        row,
        "store_type",
        "storetype",
        "store_type_desc",
        "channel",
        "outlet_type",
    )


def _infer_place_from_site(site: str) -> str:
    """
    When state/city columns are empty, derive a short human label from store/site text,
    e.g. 'SSL-GVK HYDERABAD( 122 )' -> 'GVK HYDERABAD'.
    """
    s = str(site or "").strip()
    if not s:
        return ""
    m = re.match(r"^\s*(?:SSL[-\s]+)?(.+?)\s*\(\s*\d+\s*\)\s*$", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if s.lower().startswith("ssl-"):
        return s[4:].strip()
    return s


def _replen_location_index(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Tuple[str, str]]:
    """
    Map (site, sku, size) -> (state, city) from replenishment rows for enriching recommendations.
    """
    idx: Dict[Tuple[str, str, str], Tuple[str, str]] = {}
    for r in rows:
        site = _site_value(r)
        sku = str(r.get("sku") or "").strip()
        size = str(r.get("size") or "").strip().upper()
        if not site or not sku:
            continue
        st = _row_state(r)
        ct = _row_city(r)
        key = (site, sku, size)
        prev = idx.get(key, ("", ""))
        idx[key] = (st or prev[0], ct or prev[1])
        key_any = (site, sku, "")
        prev2 = idx.get(key_any, ("", ""))
        idx[key_any] = (st or prev2[0], ct or prev2[1])
    return idx


def _lookup_loc(
    idx: Dict[Tuple[str, str, str], Tuple[str, str]],
    site: str,
    sku: str,
    size: str,
) -> Tuple[str, str]:
    sku = str(sku or "").strip()
    size_u = str(size or "").strip().upper()
    st, ct = idx.get((site, sku, size_u), ("", ""))
    if st or ct:
        return st, ct
    return idx.get((site, sku, ""), ("", ""))


def _enrich_recommendation_locations(
    rec: Dict[str, Any],
    idx: Dict[Tuple[str, str, str], Tuple[str, str]],
    site_names: Dict[str, str] | None = None,
) -> None:
    """Fill missing donor/receiver state & city; add route labels for UI."""
    donor_site = str(rec.get("donor_store") or "")
    recv_site = str(rec.get("receiver_store") or "")
    sku = str(rec.get("sku") or "")
    size = str(rec.get("size") or "")

    ds, dc = _lookup_loc(idx, donor_site, sku, size)
    rs, rc = _lookup_loc(idx, recv_site, sku, size)

    if not str(rec.get("donor_state") or "").strip():
        rec["donor_state"] = ds or rec.get("donor_state")
    if not str(rec.get("donor_city") or "").strip():
        rec["donor_city"] = dc or rec.get("donor_city")
    if not str(rec.get("receiver_state") or "").strip():
        rec["receiver_state"] = rs or rec.get("receiver_state")
    if not str(rec.get("receiver_city") or "").strip():
        rec["receiver_city"] = rc or rec.get("receiver_city")

    # Ensure store names are available even when recommendation row has only site_id/code.
    site_names = site_names or {}
    donor_site_name = site_names.get(str(donor_site).strip()) or site_names.get(str(donor_site).strip().lower())
    recv_site_name = site_names.get(str(recv_site).strip()) or site_names.get(str(recv_site).strip().lower())
    if donor_site_name:
        rec["donor_store_name"] = donor_site_name
        rec["donor_site_name"] = donor_site_name
    if recv_site_name:
        rec["receiver_store_name"] = recv_site_name
        rec["receiver_site_name"] = recv_site_name

    rec["donor_place"] = str(rec.get("donor_city") or "").strip() or _infer_place_from_site(donor_site)
    rec["receiver_place"] = str(rec.get("receiver_city") or "").strip() or _infer_place_from_site(recv_site)
    rec["transfer_route_stores"] = f"{donor_site} → {recv_site}"
    rec["transfer_route_places"] = f"{rec['donor_place'] or '—'} → {rec['receiver_place'] or '—'}"


def _map_rec_row(r: Dict[str, Any], *, priority_score_scale_hint: str | None = None) -> Dict[str, Any]:
    """Formal mapping: Table Columns -> UI Keys."""
    if not r:
        return r
    r["donor_store"] = r.get("donor_site_id") or r.get("donor_store")
    r["donor_store_name"] = r.get("donor_site_name") or r.get("donor_store_name")
    r["receiver_store"] = r.get("receiver_site_id") or r.get("receiver_store")
    r["receiver_store_name"] = r.get("receiver_site_name") or r.get("receiver_store_name")
    if r.get("receiver_soh") in (None, ""):
        if r.get("receiver_store_soh") is not None:
            r["receiver_soh"] = r.get("receiver_store_soh")

    # Priority:
    # - Some lakes store `priority_score` in [0..1]
    # - Others store it in [0..10]
    # We support an optional hint so exports/UI can use your exact scale.
    used_score = False
    ps = r.get("priority_score")
    if ps is not None and str(ps).strip() not in ("", "nan", "none", "null"):
        pf = _to_float(ps)
        if math.isfinite(pf) and pf > 0:
            used_score = True
            # If hint provided, use it even if pf happens to be <= 1.
            if priority_score_scale_hint == "0_1":
                r["priority"] = "HIGH" if pf >= 0.7 - 1e-9 else "MEDIUM" if pf >= 0.4 - 1e-9 else "LOW"
            elif priority_score_scale_hint == "0_10":
                r["priority"] = "HIGH" if pf > 7 + 1e-9 else "MEDIUM" if pf >= 4 - 1e-9 else "LOW"
            else:
                # No hint: infer from magnitude.
                if pf <= 1.0 + 1e-9:
                    # New lake style: priority_score in [0..1]
                    r["priority"] = "HIGH" if pf >= 0.7 - 1e-9 else "MEDIUM" if pf >= 0.4 - 1e-9 else "LOW"
                else:
                    # Legacy style: priority_score in [0..10] (or outside typical range)
                    r["priority"] = "HIGH" if pf > 7 + 1e-9 else "MEDIUM" if pf >= 4 - 1e-9 else "LOW"
    if not used_score:
        raw_p = _safe_int(r.get("receiver_priority") or r.get("priority"), 0)
        if raw_p >= 10:
            r["priority"] = "HIGH"
        elif 5 <= raw_p <= 9:
            r["priority"] = "MEDIUM"
        else:
            r["priority"] = "LOW"

    line_move = r.get("transfer_qty")
    if line_move in (None, ""):
        line_move = r.get("transfer_qty_total")
    r["receiver_recommended_refill"] = _safe_int(line_move or r.get("receiver_recommended_refill"))
    # UI / older clients often read `transfer_qty` only — mirror lake `transfer_qty_total` when absent.
    if r.get("transfer_qty") in (None, "") and r.get("transfer_qty_total") is not None:
        r["transfer_qty"] = r.get("transfer_qty_total")
    if r.get("warehouse_soh_after_allocation") in (None, "") and r.get("wh_soh_end_barcode") is not None:
        r["warehouse_soh_after_allocation"] = r.get("wh_soh_end_barcode")
    if r.get("size") in (None,):
        r["size"] = ""
    return r


# dbo.current_transfers managed Delta — columns match PySpark StructType in transfers tab / approve API.
# Approve paths project to _build_spark_current_transfers_row() only (avoids writing full recommendation dict).


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _value_to_naive_timestamp(v: Any, fallback: Optional[datetime]) -> Optional[datetime]:
    """Coerce lake / API values to naive datetime for Delta TimestampType."""
    if v is None or v == "":
        return fallback
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    try:
        fn = float(v)
        if not math.isfinite(fn):
            return fallback
        if fn > 1e12:
            fn /= 1000.0
        return datetime.utcfromtimestamp(fn)
    except (TypeError, ValueError, OverflowError):
        pass
    s = str(v).strip()
    if not s or s.lower() in ("nan", "none", "null", "nat"):
        return fallback
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except Exception:
        return fallback


def _spark_str(rec: Dict[str, Any], *keys: str) -> str:
    for k in keys:
        if k not in rec:
            continue
        x = rec.get(k)
        if x is None:
            continue
        t = str(x).strip()
        if t and t.lower() not in ("nan", "none", "null"):
            return t
    return ""


def _build_spark_current_transfers_row(
    mapped: Dict[str, Any],
    *,
    transfer_id: int,
    status: str,
    created_at: datetime,
    transfer_date_value: datetime,
    recommendation_created_at_raw: Any,
) -> Dict[str, Any]:
    """
    Single row matching dbo.current_transfers DDL. `mapped` must already be `_map_rec_row`-processed
    (+ optional `_enrich_recommendation_locations`) so donor/receiver_* align with donor_site_* keys.
    """
    r = mapped
    qty = r.get("transfer_qty")
    if qty in (None, ""):
        qty = r.get("transfer_qty_total")

    rc_at = _value_to_naive_timestamp(recommendation_created_at_raw, transfer_date_value)
    td = transfer_date_value
    pf = _to_float(r.get("priority_score"))

    row = {
        "transfer_id": max(0, _safe_int(transfer_id, 0)),
        "receiver_site_id": _spark_str(r, "receiver_site_id", "receiver_store"),
        "receiver_site_name": _spark_str(r, "receiver_site_name", "receiver_store_name"),
        "donor_site_id": _spark_str(r, "donor_site_id", "donor_store"),
        "donor_site_name": _spark_str(r, "donor_site_name", "donor_store_name"),
        "sku": _spark_str(r, "sku"),
        "receiver_city": _spark_str(r, "receiver_city"),
        "receiver_state": _spark_str(r, "receiver_state"),
        "donor_city": _spark_str(r, "donor_city"),
        "donor_state": _spark_str(r, "donor_state"),
        "store_type": _spark_str(r, "store_type"),
        "transfer_qty": _to_float(qty or 0),
        "match_type": _spark_str(r, "match_type"),
        "priority_score": pf,
        "status": status,
        "transfer_date": td,
        "created_at": created_at,
        "recommendation_created_at": rc_at if rc_at is not None else td,
    }
    return _dedupe_keys_case_insensitive_prefer_lower(row)


def _json_safe(value: Any) -> Any:
    # Convert NaN/Infinity to None so FastAPI JSON rendering does not fail.
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    try:
        # Handles numpy/pandas scalar NaN/NaT as well.
        if pd.isna(value):
            return None
    except Exception:
        pass
    return value


def _json_safe_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {k: _json_safe(v) for k, v in row.items()}


# Excel "Current Transfers" sheet: same column set + order as Transfers tab In transit (TransfersTab IN_TRANSIT_ONE_LAKE_COLS).
_CURRENT_TRANSIT_TAB_EXPORT_SPEC: Tuple[Tuple[str, Tuple[str, ...], str], ...] = (
    ("transfer_id", ("transfer_id", "Transfer_ID", "transfer_ID"), "txt"),
    ("receiver_site_id", ("receiver_site_id", "receiver_store"), "txt"),
    ("receiver_site_name", ("receiver_site_name", "receiver_store_name"), "txt"),
    ("donor_site_id", ("donor_site_id", "donor_store"), "txt"),
    ("donor_site_name", ("donor_site_name", "donor_store_name"), "txt"),
    ("sku", ("sku",), "txt"),
    ("receiver_city", ("receiver_city",), "txt"),
    ("receiver_state", ("receiver_state",), "txt"),
    ("donor_city", ("donor_city",), "txt"),
    ("donor_state", ("donor_state",), "txt"),
    ("store_type", ("store_type",), "txt"),
    ("transfer_qty", ("transfer_qty", "transfer_qty_total", "receiver_recommended_refill"), "qty"),
    ("match_type", ("match_type",), "txt"),
    ("priority_score", ("priority_score",), "score"),
    ("status", ("status", "transfer_status"), "txt"),
    ("transfer_date", ("transfer_date", "Transfer_Date"), "dt_or_txt"),
    ("created_at (approved)", ("created_at",), "dt"),
    ("recommendation_created_at", ("recommendation_created_at",), "dt"),
)


def _pick_in_transit_export_value(r_lc: Dict[str, Any], keys: Tuple[str, ...]) -> Any:
    for want in keys:
        kk = str(want).lower()
        if kk not in r_lc:
            continue
        v = r_lc[kk]
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _excel_ts_display(v: Any) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = _value_to_naive_timestamp(v, None)
        if dt is not None:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        pass
    s = str(v).strip()
    return s


def _export_in_transit_cell(v: Any, kind: str) -> Any:
    if kind == "txt":
        return "" if v is None else str(v)
    if kind == "qty":
        if v is None:
            return ""
        try:
            x = float(v)
            if not math.isfinite(x):
                return ""
            if abs(x - round(x)) < 1e-9:
                return int(round(x))
            return round(x, 2)
        except Exception:
            return ""
    if kind == "score":
        if v is None or v == "":
            return ""
        try:
            return round(float(v), 2)
        except Exception:
            return ""
    if kind == "dt":
        return _excel_ts_display(v)
    if kind == "dt_or_txt":
        if v is None:
            return ""
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%d %H:%M:%S")
        s = str(v).strip()
        if not s:
            return ""
        dt = _value_to_naive_timestamp(v, None)
        if dt is not None:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return s
    return "" if v is None else str(v)


def _dataframe_current_transfers_in_transit_tab(rows_raw: List[Dict[str, Any]]) -> pd.DataFrame:
    """
    Build Current Transfers sheet with only the columns shown on the Transfers > In transit tab
    (mapping + enrich parity with GET /transfers-recom/current).
    """
    headers = [h for h, _, _ in _CURRENT_TRANSIT_TAB_EXPORT_SPEC]
    if not rows_raw:
        return pd.DataFrame(columns=headers)

    try:
        replen_rows = _read_transfer_table(TRANSFER_SOURCE_TABLE)
        loc_idx = _replen_location_index(replen_rows) if replen_rows else {}
        site_name_idx = _site_name_index(replen_rows) if replen_rows else {}
    except Exception:
        loc_idx = {}
        site_name_idx = {}

    out: List[Dict[str, Any]] = []
    for raw in rows_raw:
        rr = dict(raw)
        _map_rec_row(rr)
        _enrich_recommendation_locations(rr, loc_idx, site_name_idx)
        rr = _json_safe_row(rr)
        r_lc = {str(k).lower(): v for k, v in rr.items()}
        row_out: Dict[str, Any] = {}
        for hdr, keys, kind in _CURRENT_TRANSIT_TAB_EXPORT_SPEC:
            v = _pick_in_transit_export_value(r_lc, keys)
            row_out[hdr] = _export_in_transit_cell(v, kind)
        out.append(row_out)

    df = pd.DataFrame(out, columns=headers)
    try:
        for col in df.select_dtypes(include=["datetimetz"]).columns:
            df[col] = df[col].dt.tz_localize(None)
    except Exception:
        pass
    return df


def _to_float(v: Any) -> float:
    try:
        if v is None:
            return 0.0
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("", "nan", "none", "null"):
                return 0.0
            x = float(s)
        else:
            x = float(v)
        try:
            if pd.isna(x):
                return 0.0
        except Exception:
            pass
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return 0.0
        return x
    except Exception:
        return 0.0


def _to_int(v: Any) -> int:
    x = _to_float(v)
    try:
        return int(x)
    except (ValueError, OverflowError):
        return 0


def _is_missing_transfer_date(row: Dict[str, Any]) -> bool:
    raw = (
        row.get("last_transfer_date")
        or row.get("Last_Transfer_Date")
        or row.get("last_transfer_dt")
        or row.get("Last_Transfer_Dt")
    )
    if raw is None:
        return True
    text = str(raw).strip().lower()
    return text in ("", "nan", "none", "null", "nat")


def _build_zero_soh_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    total_soh = 0
    total_soh_no_transfer_date = 0
    total_revenue = 0.0
    sku_rollup: Dict[str, Dict[str, Any]] = {}
    site_rollup: Dict[str, Dict[str, Any]] = {}

    for row in rows:
        sku = str(row.get("sku") or row.get("SKU") or "").strip()
        soh = _to_int(row.get("store_soh") or row.get("Store_Soh"))
        amt = _to_float(row.get("total_amt") or row.get("Total_Amt"))
        total_soh += soh
        if _is_missing_transfer_date(row):
            total_soh_no_transfer_date += soh
        total_revenue += amt
        site = str(row.get("site") or row.get("Site") or row.get("site_id") or row.get("Site_ID") or "").strip()
        if site:
            site_item = site_rollup.setdefault(site, {"site": site, "total_stock_amount": 0.0, "total_soh": 0})
            site_item["total_stock_amount"] += amt
            site_item["total_soh"] += soh
        if not sku:
            continue
        item = sku_rollup.setdefault(sku, {"sku": sku, "total_soh": 0, "total_revenue": 0.0, "soh_count": 0, "total_stock_amount": 0.0})
        item["total_soh"] += soh
        item["total_revenue"] += amt
        item["total_stock_amount"] += amt
        item["soh_count"] += 1

    top_skus = sorted(sku_rollup.values(), key=lambda x: x["total_soh"], reverse=True)[:10]
    for s in top_skus:
        s["total_revenue"] = round(float(s["total_revenue"]), 2)
        s["total_stock_amount"] = round(float(s["total_stock_amount"]), 2)
    top_skus_by_amount = sorted(sku_rollup.values(), key=lambda x: x["total_stock_amount"], reverse=True)[:5]
    for s in top_skus_by_amount:
        s["total_revenue"] = round(float(s["total_revenue"]), 2)
        s["total_stock_amount"] = round(float(s["total_stock_amount"]), 2)

    top_sites_by_amount = sorted(site_rollup.values(), key=lambda x: x["total_stock_amount"], reverse=True)[:5]
    for s in top_sites_by_amount:
        s["total_stock_amount"] = round(float(s["total_stock_amount"]), 2)

    return {
        "total_soh": int(total_soh),
        "total_soh_no_transfer_date": int(total_soh_no_transfer_date),
        "total_soh_with_transfer_date": int(total_soh - total_soh_no_transfer_date),
        "total_revenue": round(float(total_revenue), 2),
        "top_skus": top_skus,
        "top_sites_by_amount": top_sites_by_amount,
        "top_skus_by_amount": top_skus_by_amount,
    }


def _read_transfer_table(table_name: str, *, bypass_cache: bool = False) -> List[Dict[str, Any]]:
    """
    For transfer working tables, prefer managed Delta (Tables/dbo/*), else Files JSON.
    Results are cached ~120s unless bypass_cache=True (e.g. /current?refresh=1).
    """
    now = time.time()
    if not bypass_cache:
        cached_rows = _TRANSFER_TABLE_CACHE.get(table_name)
        cached_at = _TRANSFER_TABLE_CACHE_AT.get(table_name, 0.0)
        if cached_rows is not None and (now - cached_at) < _TRANSFER_TABLE_CACHE_TTL_SECONDS:
            return cached_rows

    # Table-first mode (Lakehouse managed tables).
    #
    # Do NOT merge Files/.../current_transfers.json after read_delta_table() succeeds with []
    # — dbo.current_transfers was often cleared while the emergency JSON still held rows, so “In transit”
    # showed phantom transfers. Rows from JSON remain available only via the exception path below
    # when Delta itself cannot be read (same as other tables).
    try:
        rows = store.read_delta_table(table_name)
        normalized = [_normalize_row(r) for r in rows]
        _TRANSFER_TABLE_CACHE[table_name] = normalized
        _TRANSFER_TABLE_CACHE_AT[table_name] = now
        return normalized
    except Exception:
        pass
    try:
        rows = store.read_table(table_name)
        normalized = [_normalize_row(r) for r in rows]
        _TRANSFER_TABLE_CACHE[table_name] = normalized
        _TRANSFER_TABLE_CACHE_AT[table_name] = now
        return normalized
    except Exception:
        return []


def _write_transfer_table(table_name: str, rows: List[Dict[str, Any]]) -> None:
    """
    Write transfer datasets directly to Lakehouse tables, fallback to Files.
    """
    try:
        store.replace_delta_table(table_name, rows)
    except Exception:
        store.replace_table(table_name, rows)
    # Invalidate and warm cache with fresh rows
    normalized = [_normalize_row(r) for r in rows]
    _TRANSFER_TABLE_CACHE[table_name] = normalized
    _TRANSFER_TABLE_CACHE_AT[table_name] = time.time()


def _receiver_residual_need_qty(receiver: Dict[str, Any]) -> int:
    """
    Units still needed for transfer scoring / qty caps.
    When sync stored `refill_is_net_unfilled`, `recommended_refill` is already lake unfilled (net of store+WH)
    — do not subtract store_soh again, and do not floor at 1 (that made every line look like need=1).
    Legacy path: gross recommended_refill minus store SOH (min 0).
    """
    refill = _safe_int(receiver.get("recommended_refill"))
    if receiver.get("refill_is_net_unfilled"):
        return max(refill, 0)
    return max(refill - _safe_qty(receiver.get("store_soh")), 0)


def _build_recommendations(donors: List[Dict[str, Any]], receivers: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    donor_map: Dict[tuple, List[Dict[str, Any]]] = {}
    donor_stack_sizes: Dict[tuple, set] = {}
    donor_qty_by_size: Dict[tuple, int] = {}
    receiver_needed_sizes: Dict[tuple, set] = {}

    for donor in donors:
        sku = str(donor.get("sku") or "").strip()
        size = str(donor.get("size") or "").upper().strip()
        site = _site_value(donor)
        if not sku or not size or not site:
            continue
        donor_map.setdefault((sku, size), []).append(donor)
        if _safe_qty(donor.get("store_soh")) > 0:
            donor_stack_sizes.setdefault((site, sku), set()).add(size)
            donor_qty_by_size[(site, sku, size)] = _safe_qty(donor.get("store_soh"))

    for receiver in receivers:
        sku = str(receiver.get("sku") or "").strip()
        size = str(receiver.get("size") or "").upper().strip()
        site = _site_value(receiver)
        if not sku or not size or not site:
            continue
        need_qty = _receiver_residual_need_qty(receiver)
        if need_qty > 0:
            receiver_needed_sizes.setdefault((site, sku), set()).add(size)

    # Highest need first
    sorted_receivers = sorted(
        receivers,
        key=lambda r: (
            _receiver_residual_need_qty(r),
            -_safe_qty(r.get("store_soh")),
        ),
        reverse=True,
    )

    recommendations: List[Dict[str, Any]] = []
    now = OneLakeJsonStore.now_iso()
    next_id = 1
    used_donor_size_pool: Dict[tuple, int] = {}

    for receiver in sorted_receivers:
        r_sku = str(receiver.get("sku") or "").strip()
        r_size = str(receiver.get("size") or "").upper().strip()
        r_site = _site_value(receiver)
        if not r_sku or not r_size or not r_site:
            continue

        needed_qty = _receiver_residual_need_qty(receiver)
        if needed_qty <= 0:
            continue
        needed_sizes = receiver_needed_sizes.get((r_site, r_sku), {r_size})
        matches = donor_map.get((r_sku, r_size), [])
        if not matches:
            continue

        best = None
        best_score = -10**9

        for donor in matches:
            donor_site = _site_value(donor)
            if not donor_site or donor_site == r_site:
                continue
            if str(donor.get("store_type") or "") != str(receiver.get("store_type") or ""):
                continue

            donor_size_key = (donor_site, r_sku, r_size)
            allocated = used_donor_size_pool.get(donor_size_key, 0)
            available = max(_safe_qty(donor.get("store_soh")) - allocated - 1, 0)  # keep at least 1 unit safety
            if available <= 0:
                continue

            donor_sizes = donor_stack_sizes.get((donor_site, r_sku), set())
            covered = len(needed_sizes.intersection(donor_sizes))
            total_needed_sizes = max(len(needed_sizes), 1)
            coverage_ratio = covered / total_needed_sizes

            score = 0
            score += min(needed_qty, 10) * 10  # demand priority
            score += min(available, 10) * 4     # supply capability
            if coverage_ratio >= 1.0:
                score += 50                      # full stack support
            elif coverage_ratio >= 0.7:
                score += 25
            elif coverage_ratio >= 0.4:
                score += 10
            if str(donor.get("state") or "") and str(donor.get("state") or "") == str(receiver.get("state") or ""):
                score += 8                       # same-state logistics preference

            if score > best_score:
                best_score = score
                best = donor

        if not best:
            continue

        donor_site = _site_value(best)
        donor_sizes = donor_stack_sizes.get((donor_site, r_sku), set())
        covered = len(needed_sizes.intersection(donor_sizes))
        total_needed_sizes = max(len(needed_sizes), 1)
        full_stack_possible = covered >= total_needed_sizes

        donor_size_key = (donor_site, r_sku, r_size)
        allocated = used_donor_size_pool.get(donor_size_key, 0)
        available = max(_safe_qty(best.get("store_soh")) - allocated - 1, 0)
        transfer_units = max(min(needed_qty, available), 1)
        used_donor_size_pool[donor_size_key] = allocated + transfer_units

        # Bundle behavior: required size must transfer; send 1 unit of other sizes if available.
        additional_sizes_sent: List[str] = []
        additional_units = 0
        for sz in sorted(donor_sizes):
            if sz == r_size:
                continue
            key = (donor_site, r_sku, sz)
            raw_qty = donor_qty_by_size.get(key, 0)
            alloc_sz = used_donor_size_pool.get(key, 0)
            avail_sz = max(raw_qty - alloc_sz - 1, 0)  # keep donor safety 1
            if avail_sz <= 0:
                continue
            used_donor_size_pool[key] = alloc_sz + 1
            additional_sizes_sent.append(sz)
            additional_units += 1

        priority = "HIGH" if _safe_qty(receiver.get("store_soh")) <= 0 or needed_qty >= 3 else "MEDIUM"
        recommendations.append(
            {
                "id": next_id,
                "transfer_id": next_id,
                "transfer_status": "PENDING",
                "sku": r_sku,
                "size": receiver.get("size"),
                "barcode": best.get("barcode"),
                "donor_store": donor_site,
                "donor_site_id": best.get("site_id") or donor_site,
                "donor_site": str(best.get("site_name") or best.get("site") or donor_site),
                "donor_store_name": str(best.get("site_name") or best.get("site") or donor_site),
                "donor_state": _row_state(best) or best.get("state"),
                "donor_city": _row_city(best) or best.get("city"),
                "donor_soh": _safe_qty(best.get("store_soh")),
                "donor_store_soh": _safe_qty(best.get("store_soh")),
                "donor_wh_soh": _safe_qty(best.get("wh_soh")),
                "donor_recommended_refill": _safe_int(best.get("recommended_refill")),
                "donor_department": best.get("department"),
                "donor_season": best.get("season"),
                "donor_store_type": best.get("store_type"),
                "donor_days_since_last_sale": _safe_int(best.get("days_since_last_sale")),
                "donor_days_from_first_transfer_date": _safe_int(best.get("days_since_last_transfer")),
                "donor_sold_qty_last_5w": _safe_int(best.get("sold_qty_prev_5w")),
                "donor_net_sales_last_35d": _safe_int(best.get("net_sales_last_35d")),
                "donor_selling_rate": 0.0,
                "receiver_store": r_site,
                "receiver_site_id": receiver.get("site_id") or r_site,
                "receiver_site": str(receiver.get("site_name") or receiver.get("site") or r_site),
                "receiver_store_name": str(receiver.get("site_name") or receiver.get("site") or r_site),
                "receiver_state": _row_state(receiver) or receiver.get("state"),
                "receiver_city": _row_city(receiver) or receiver.get("city"),
                "receiver_soh": _safe_qty(receiver.get("store_soh")),
                "receiver_store_soh": _safe_qty(receiver.get("store_soh")),
                "receiver_wh_soh": _safe_qty(receiver.get("wh_soh")),
                "receiver_department": receiver.get("department"),
                "receiver_season": receiver.get("season"),
                "receiver_store_type": receiver.get("store_type"),
                "receiver_days_since_last_sale": _safe_int(receiver.get("days_since_last_sale")),
                "receiver_days_from_first_transfer_date": _safe_int(receiver.get("days_since_last_transfer")),
                "receiver_sold_qty_last_5w": _safe_int(receiver.get("sold_qty_prev_5w")),
                "receiver_net_sales_last_35d": _safe_int(receiver.get("net_sales_last_35d")),
                "receiver_selling_rate": 0.0,
                "receiver_recommended_refill": _safe_int(receiver.get("recommended_refill")),
                "receiver_refill_is_net_unfilled": bool(receiver.get("refill_is_net_unfilled")),
                "transfer_stacks": 1 if full_stack_possible else 0,
                "required_size_units": transfer_units,
                "additional_size_units": additional_units,
                "additional_sizes_sent": additional_sizes_sent,
                "total_units": transfer_units + additional_units,
                "priority": priority,
                "stack_coverage": round((covered / total_needed_sizes) * 100, 1),
                "status": "PENDING",
                "created_at": now,
            }
        )
        next_id += 1
        if len(recommendations) >= 200:
            break

    return recommendations

@router.get("/debug-cols")
async def debug_lakehouse_cols():
    try:
        rows = _read_transfer_table(TRANSFER_SOURCE_TABLE)
        cols = list(rows[0].keys()) if rows else []
        return {"columns": cols}
    except Exception as e:
        return {"error": str(e)}

@router.post("/sync")
async def sync_transfers_data():
    """
    Simplified sync: Logic moved to Fabric Notebook.
    This endpoint now just acts as a placeholder or can be used to trigger the notebook later.
    """
    try:
        logger.info("Sync triggered. Refreshing data from Lakehouse tables...")
        # Clear local caches to ensure fresh data is fetched from Lakehouse
        _TRANSFER_TABLE_CACHE.clear()
        _TRANSFER_TABLE_CACHE_AT.clear()
        
        return {
            "status": "success",
            "message": "Data refreshed from Lakehouse. (Notebook-driven pipeline)",
            "source_table": TRANSFER_SOURCE_TABLE
        }
    except Exception as e:
        logger.error(f"Sync refresh error: {e}")
        return {"status": "error", "message": f"Sync failed: {str(e)}"}

        return {
            "status": "success",
            "receivers": len(receivers),
            "donors": len(donors),
            "recommendations": len(recommendations),
            "source_table": TRANSFER_SOURCE_TABLE,
            "refill_source_filter": TRANSFER_SYNC_REFILL_SOURCE or None,
        }
    except Exception as e:
        logger.error(f"Sync error: {e}", exc_info=True)
        return {"status": "error", "message": f"Sync failed: {str(e)}"}

@router.get("/recommendations")
async def get_recommended_transfers(
    donor_site: str = None,
    donor_sku: str = None,
    recv_site: str = None,
    recv_sku: str = None,
    recv_site_id: str = None,
    recv_month: str = None,
    recv_week: str = None,
    limit: int | None = Query(
        None,
        ge=1,
        le=20_000,
        description="Page size. Omit to return the full list (default / legacy).",
    ),
    offset: int = Query(0, ge=0, description="Row offset for paginated response."),
):
    try:
        recs_all = _read_transfer_table(RECOMMENDED_TRANSFERS_TABLE)
        receiver_rows = _read_transfer_table("potential_receivers")
        receiver_refill_idx = _receiver_refill_index(receiver_rows)
        recs_all = [
            r
            for r in recs_all
            if (
                not donor_site
                or donor_site in ("All Sites", "All Stores", "All")
                or str(r.get("donor_store") or r.get("donor_site_id") or "").strip() == str(donor_site or "").strip()
            )
            and (not donor_sku or donor_sku in ("All SKUs", "All") or str(r.get("sku") or "") == donor_sku)
        ]
        recs_all = [r for r in recs_all if str(r.get("status", "PENDING")).upper() == "PENDING"]

        # Project decision: treat priority_score as 0..10 scale only.
        priority_scale_hint = "0_10"
        try:
            replen_rows = _read_transfer_table(TRANSFER_SOURCE_TABLE)
            loc_idx = _replen_location_index(replen_rows) if replen_rows else {}
            site_name_idx = _site_name_index(replen_rows) if replen_rows else {}
            lake_idx = _replen_receiver_lake_index(replen_rows) if replen_rows else {}
        except Exception:
            loc_idx = {}
            site_name_idx = {}
            lake_idx = {}
        for idx, r in enumerate(recs_all, start=1):
            # Assign virtual ID if not present in Lakehouse
            if "id" not in r:
                r["id"] = idx
                
            # Apply formal mapping from Lakehouse columns
            _map_rec_row(r, priority_score_scale_hint=priority_scale_hint)
            
            _enrich_recommendation_locations(r, loc_idx, site_name_idx)
            # Backfill refill when old recommendation rows don't carry this field.
            refill = _safe_int(r.get("receiver_recommended_refill"), default=-1)
            if refill <= 0:
                r["receiver_recommended_refill"] = _lookup_receiver_refill_from_index(receiver_refill_idx, r)
            _enrich_rec_from_receiver_lake(r, lake_idx)
            r["movement_units"] = _movement_units_for_rec(r)
        # Receiver filters after OneLake enrich so week/month can use receiver_lake_* fields.
        recs = [
            r
            for r in recs_all
            if _transfer_row_matches_recv_filters(
                r,
                recv_site=recv_site,
                recv_site_id=recv_site_id,
                recv_sku=recv_sku,
                recv_month=recv_month,
                recv_week=recv_week,
            )
        ]
        # Ensure every recommendation has a stable numeric id for approve API.
        for pos, r in enumerate(recs, start=1):
            rid = r.get("id")
            if rid is None or str(rid).strip() == "":
                r["id"] = pos
            else:
                r["id"] = _safe_int(rid, pos)

        def _priority_rank(r: Dict[str, Any]) -> int:
            """
            Sort order for UI:
              HIGH > MEDIUM > LOW
            We avoid lexicographic reverse ordering (which can place MEDIUM above HIGH).
            """
            p_raw = r.get("priority")
            p = str(p_raw or "").strip().upper()
            if p.startswith("HIGH"):
                return 3
            if p.startswith("MED"):
                return 2
            if p.startswith("LOW"):
                return 1

            # Fallback: if we only have numeric score, rank it.
            ps = r.get("priority_score", r.get("receiver_priority"))
            try:
                if ps is not None and str(ps).strip() != "":
                    v = float(ps)
                    if not math.isfinite(v):
                        return 0
                    # Typical lake ranges: 0..1 for score; also legacy could be 0..10.
                    if v >= 0.66 or v >= 6.6:
                        return 3
                    if v >= 0.33 or v >= 3.3:
                        return 2
                    if v >= 0.01 or v >= 0.1:
                        return 1
            except Exception:
                pass
            return 0

        recs.sort(
            key=lambda x: (_priority_rank(x), str(x.get("created_at", ""))),
            reverse=True,
        )
        rec_total = len(recs)
        paged = limit is not None
        if paged:
            page = recs[offset : offset + int(limit or 0)]
            has_more = offset + len(page) < rec_total
        else:
            page = recs
            has_more = False
        # Fix: ensure NaN/Inf values are JSON-compliant
        safe_recs = [_json_safe_row(r) for r in page]
        out: Dict[str, Any] = {"status": "success", "data": safe_recs, "total": rec_total}
        if paged:
            out["limit"] = limit
            out["offset"] = offset
            out["has_more"] = has_more
        return out
    except Exception as e:
        return {"status": "error", "message": str(e), "data": []}

@router.get("/filters/sites")
async def get_transfers_sites():
    try:
        recv = _read_transfer_table("potential_receivers")
        donor = _read_transfer_table("potential_donors")
        sites = sorted({_site_value(r) for r in [*recv, *donor] if _site_value(r)})
        return [{"id": "All Sites", "label": "All Sites"}] + [{"id": s, "label": s} for s in sites]
    except Exception:
        return [{"id": "All Sites", "label": "All Sites"}]

@router.get("/filters/skus")
async def get_transfers_skus():
    try:
        recv = _read_transfer_table("potential_receivers")
        donor = _read_transfer_table("potential_donors")
        skus = sorted({str(r.get("sku")) for r in [*recv, *donor] if r.get("sku") is not None})
        return [{"id": "All SKUs", "label": "All SKUs"}] + [{"id": s, "label": s} for s in skus]
    except Exception:
        return [{"id": "All SKUs", "label": "All SKUs"}]

@router.get("/data")
async def get_transfers_data(
    recv_site: str = None, recv_sku: str = None,
    donor_site: str = None, donor_sku: str = None,
    r_off: int = Query(0, ge=0, description="Offset into sorted receivers (paged mode)."),
    d_off: int = Query(0, ge=0, description="Offset into sorted donors (paged mode)."),
    r_limit: int | None = Query(
        None,
        ge=0,
        le=10_000,
        description="Receivers per page. Omit for full list (legacy). 0 = return no receiver rows (client finished this side).",
    ),
    d_limit: int | None = Query(
        None,
        ge=0,
        le=10_000,
        description="Donors per page. Omit for full list (legacy). 0 = return no donor rows (client finished this side).",
    ),
):
    try:
        receiver_rows = _read_transfer_table("potential_receivers")
        donor_rows = _read_transfer_table("potential_donors")

        def row_match(row: Dict[str, Any], site: str = None, sku: str = None) -> bool:
            row_site = str(row.get("site_id") or row.get("site") or "").strip()
            if site and site not in ("All Sites", "All Stores", "All"):
                if row_site != site and str(row.get("site_name") or "").strip() != site:
                    return False
            if sku and sku not in ("All SKUs", "All"):
                if str(row.get("sku") or "").strip() != sku:
                    return False
            return True

        receivers = []
        for r in receiver_rows:
            if not row_match(r, recv_site, recv_sku):
                continue
            if not _row_classification_ok(r, "receiver"):
                continue

            need = _row_refill_for_potential(r)
            if need <= 0:
                continue

            d_sl = _pick_int(
                r,
                "days_since_last_sale",
                "days_from_last_selling_date",
                "days_since_last_sales",
                default=0,
            )
            ts = _pick_int(
                r,
                "sum_of_sales",
                "total_sales",
                "Sum_of_Sales",
                "Total_Sales",
                default=0,
            )
            receivers.append(
                _json_safe_row(
                    {
                        "site_id": str(r.get("site_id") or r.get("site") or "").strip(),
                        "site_name": str(
                            r.get("site_name") or r.get("site_id") or "Unknown"
                        ).strip()
                        or "Unknown",
                        "store_type": _row_store_type(r),
                        "city": _row_city(r),
                        "state": _row_state(r),
                        "sku": str(r.get("sku") or "").strip(),
                        "size": str(r.get("size") or "").strip(),
                        "store_soh": _safe_qty(r.get("store_soh")),
                        "wh_soh": _safe_qty(r.get("wh_soh")),
                        "days_since_last_sale": d_sl,
                        "last_week_sales": _pick_last_week_sold_qty(r),
                        "last_35day_sold_qty": _pick_sold_qty_last_35d(r),
                        "total_sales": ts,
                        "qty_still_needed": need,
                        "priority_score": _row_priority_score_value(r),
                    }
                )
            )

        receivers.sort(key=lambda x: (x.get("store_soh", 0), x.get("wh_soh", 0)))

        donors = []
        for r in donor_rows:
            if not row_match(r, donor_site, donor_sku):
                continue
            if not _row_classification_ok(r, "donor"):
                continue

            ts = _pick_int(
                r,
                "sum_of_sales",
                "total_sales",
                "Sum_of_Sales",
                "Total_Sales",
                default=0,
            )
            donors.append(
                _json_safe_row(
                    {
                        "site_id": str(r.get("site_id") or r.get("site") or "").strip(),
                        "site_name": str(
                            r.get("site_name") or r.get("site_id") or "Unknown"
                        ).strip()
                        or "Unknown",
                        "store_type": _row_store_type(r),
                        "city": _row_city(r),
                        "state": _row_state(r),
                        "sku": str(r.get("sku") or "").strip(),
                        "size": str(r.get("size") or "").strip(),
                        "store_soh": _safe_qty(r.get("store_soh")),
                        "wh_soh": _safe_qty(r.get("wh_soh")),
                        "days_from_last_transfer": _row_days_last_transfer(r),
                        "days_from_last_sales": _row_days_last_selling(r),
                        "last_week_sales": _pick_last_week_sold_qty(r),
                        "last_35day_sold_qty": _pick_sold_qty_last_35d(r),
                        "total_sales": ts,
                        "donor_source": str(r.get("donor_source") or "").strip(),
                    }
                )
            )

        donors.sort(
            key=lambda x: (x.get("days_from_last_sales", 0), x.get("days_from_last_transfer", 0)),
            reverse=True,
        )

        paged = r_limit is not None or d_limit is not None
        if not paged:
            return {"receivers": receivers, "donors": donors, "status": "success"}

        recv_total = len(receivers)
        donor_total = len(donors)

        if r_limit is not None:
            if r_limit > 0:
                out_recv = receivers[r_off : r_off + r_limit]
                recv_has_more = r_off + r_limit < recv_total
            else:
                out_recv = []
                recv_has_more = False
        else:
            out_recv = list(receivers)
            recv_has_more = False

        if d_limit is not None:
            if d_limit > 0:
                out_donor = donors[d_off : d_off + d_limit]
                donor_has_more = d_off + d_limit < donor_total
            else:
                out_donor = []
                donor_has_more = False
        else:
            out_donor = list(donors)
            donor_has_more = False

        return {
            "receivers": out_recv,
            "donors": out_donor,
            "status": "success",
            "receivers_total": recv_total,
            "donors_total": donor_total,
            "receivers_has_more": recv_has_more,
            "donors_has_more": donor_has_more,
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "receivers": [], "donors": []}

@router.post("/approve/{rec_id}")
async def approve_transfer(rec_id: int):
    try:
        recs = _read_transfer_table(RECOMMENDED_TRANSFERS_TABLE)
        rec_raw = None
        
        # Try finding by literal 'id' or row index as fallback
        for idx, r in enumerate(recs, start=1):
            effective_id = _safe_int(r.get("id"), idx)
            if effective_id == rec_id:
                rec_raw = r
                break
        
        if not rec_raw:
            return {"status": "error", "message": "Recommendation not found."}

        # Use formal mapping + lake enrich so *_city/*_state/site names match dbo.current_transfers columns.
        rec = _map_rec_row(dict(rec_raw))
        recommendation_created_at = rec_raw.get("created_at")
        try:
            replen_rows = _read_transfer_table(TRANSFER_SOURCE_TABLE)
            loc_idx = _replen_location_index(replen_rows) if replen_rows else {}
            site_name_idx = _site_name_index(replen_rows) if replen_rows else {}
        except Exception:
            loc_idx = {}
            site_name_idx = {}
        _enrich_recommendation_locations(rec, loc_idx, site_name_idx)

        # 1. Add to current_transfers (project to Spark dbo.current_transfers schema only)
        current = _read_transfer_table("current_transfers")
        existing_transfer_ids: List[int] = []
        for row in current:
            raw_tid = row.get("Transfer_ID", row.get("transfer_id"))
            tid = _safe_int(raw_tid, 0)
            if tid > 0:
                existing_transfer_ids.append(tid)
        next_transfer_id = (max(existing_transfer_ids) + 1) if existing_transfer_ids else 1
        
        ts = _utc_naive_now()
        new_transfer = _build_spark_current_transfers_row(
            rec,
            transfer_id=next_transfer_id,
            status="IN_TRANSIT",
            created_at=ts,
            transfer_date_value=ts,
            recommendation_created_at_raw=recommendation_created_at,
        )
        current.append(new_transfer)
        store.replace_delta_table("current_transfers", current)
        _TRANSFER_TABLE_CACHE["current_transfers"] = [_normalize_row(r) for r in current]
        _TRANSFER_TABLE_CACHE_AT["current_transfers"] = time.time()

        # 2. DELETE from recommended table, potential_donors, and potential_receivers
        sku = str(rec.get("sku") or "").strip()
        size = str(rec.get("size") or "").strip().upper()
        donor_site = str(rec.get("donor_store") or "").strip()
        receiver_site = str(rec.get("receiver_store") or "").strip()

        # Update Recommended Transfers (literal delete)
        new_recs = [r for r in recs if _safe_int(r.get("id")) != rec_id]
        _write_transfer_table(RECOMMENDED_TRANSFERS_TABLE, new_recs)

        def _row_matches_site_sku(
            row: Dict[str, Any], *, want_sku: str, want_size: str, want_site: str
        ) -> bool:
            d = _normalize_row(row)
            if d.get("sku") != want_sku:
                return False
            site_val = str(d.get("site") or d.get("site_id") or "").strip()
            if site_val != want_site:
                return False
            if not want_size:
                return True
            return str(d.get("size") or "").strip().upper() == want_size

        # Update Potential Donors
        donors = _read_transfer_table("potential_donors")
        new_donors = [
            d
            for d in donors
            if not _row_matches_site_sku(
                d, want_sku=sku, want_size=size, want_site=donor_site
            )
        ]
        _write_transfer_table("potential_donors", new_donors)

        # Update Potential Receivers
        receivers = _read_transfer_table("potential_receivers")
        new_receivers = [
            r
            for r in receivers
            if not _row_matches_site_sku(
                r, want_sku=sku, want_size=size, want_site=receiver_site
            )
        ]
        _write_transfer_table("potential_receivers", new_receivers)

        return {"status": "success", "message": "Transfer approved and inventory lists updated."}
    except Exception as e:
        logger.error(f"Approval error: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}


@router.post("/approve-all")
async def approve_all_transfers():
    try:
        recs = _read_transfer_table(RECOMMENDED_TRANSFERS_TABLE)
        pending_recs = [r for r in recs if str(r.get("status", "PENDING")).upper() == "PENDING"]
        if not pending_recs:
            return {"status": "success", "message": "No pending recommendations found.", "approved_count": 0}

        current = _read_transfer_table("current_transfers")
        existing_transfer_ids: List[int] = []
        for row in current:
            raw_tid = row.get("Transfer_ID", row.get("transfer_id"))
            tid = _safe_int(raw_tid, 0)
            if tid > 0:
                existing_transfer_ids.append(tid)
        next_transfer_id = (max(existing_transfer_ids) + 1) if existing_transfer_ids else 1

        try:
            replen_rows = _read_transfer_table(TRANSFER_SOURCE_TABLE)
            loc_idx = _replen_location_index(replen_rows) if replen_rows else {}
            site_name_idx = _site_name_index(replen_rows) if replen_rows else {}
        except Exception:
            loc_idx = {}
            site_name_idx = {}

        approved_count = 0
        for idx, rec in enumerate(recs, start=1):
            if str(rec.get("status", "PENDING")).upper() != "PENDING":
                continue

            matched_effective_id = _safe_int(rec.get("id"), idx)
            if any(_safe_int(r.get("source_rec_id")) == matched_effective_id for r in current):
                rec["id"] = matched_effective_id
                rec["status"] = "APPROVED"
                rec["approved_at"] = OneLakeJsonStore.now_iso()
                continue

            mapped = _map_rec_row(dict(rec))
            enriched = dict(mapped)
            _enrich_recommendation_locations(enriched, loc_idx, site_name_idx)
            ts = _utc_naive_now()
            spark_row = _build_spark_current_transfers_row(
                enriched,
                transfer_id=next_transfer_id,
                status="IN_TRANSIT",
                created_at=ts,
                transfer_date_value=ts,
                recommendation_created_at_raw=mapped.get("created_at"),
            )
            next_transfer_id += 1
            current.append(spark_row)
            rec["id"] = matched_effective_id
            rec["status"] = "APPROVED"
            rec["approved_at"] = OneLakeJsonStore.now_iso()
            approved_count += 1

        try:
            store.replace_delta_table("current_transfers", current)
            _TRANSFER_TABLE_CACHE["current_transfers"] = [_normalize_row(r) for r in current]
            _TRANSFER_TABLE_CACHE_AT["current_transfers"] = time.time()
        except Exception as e:
            logger.error(f"Failed writing dbo.current_transfers on approve-all: {e}", exc_info=True)
            return {"status": "error", "message": f"Failed to save approved transfers in dbo.current_transfers: {str(e)}"}

        _write_transfer_table(RECOMMENDED_TRANSFERS_TABLE, recs)
        return {"status": "success", "message": "All pending transfers approved.", "approved_count": approved_count}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def _potential_donor_export_row(r: Dict[str, Any]) -> Dict[str, Any]:
    nr = _normalize_row(_dedupe_keys_case_insensitive_prefer_lower(dict(r)))
    return {
        "site_id": str(nr.get("site_id") or nr.get("site") or "").strip(),
        "site_name": str(nr.get("site_name") or nr.get("site") or "").strip(),
        "barcode": str(nr.get("barcode") or ""),
        "sku": str(nr.get("sku") or ""),
        "size": str(nr.get("size") or ""),
        "store_soh": _safe_qty(nr.get("store_soh")),
        "wh_soh": _safe_qty(nr.get("wh_soh")),
        "city": _row_city(nr),
        "state": _row_state(nr),
        "store_type": _row_store_type(nr),
        "store_grade": str(nr.get("store_grade") or "").strip(),
        "days_from_last_sales": _row_days_last_selling(nr),
        "days_from_last_transfer": _row_days_last_transfer(nr),
        "last_week_sales": _pick_last_week_sold_qty(nr),
        "last_35day_sold_qty": _pick_sold_qty_last_35d(nr),
        "total_sales": _pick_int(nr, "sum_of_sales", "total_sales", "total_net_sales", default=0),
        "donor_source": str(nr.get("donor_source") or "").strip(),
    }


def _potential_receiver_export_row(r: Dict[str, Any]) -> Dict[str, Any]:
    nr = _normalize_row(_dedupe_keys_case_insensitive_prefer_lower(dict(r)))
    need = _row_refill_for_potential(nr)
    rec_send = _pick_int(
        nr,
        "recommended_qty_to_send",
        "receiver_recommended_refill",
        "recommended_refill",
        "recommendation_refill",
        default=0,
    )
    if rec_send <= 0 and need > 0:
        rec_send = need
    return {
        "site_id": str(nr.get("site_id") or nr.get("site") or "").strip(),
        "site_name": str(nr.get("site_name") or nr.get("site") or "").strip(),
        "barcode": str(nr.get("barcode") or ""),
        "sku": str(nr.get("sku") or ""),
        "size": str(nr.get("size") or ""),
        "store_soh": _safe_qty(nr.get("store_soh")),
        "wh_soh": _safe_qty(nr.get("wh_soh")),
        "city": _row_city(nr),
        "state": _row_state(nr),
        "store_type": _row_store_type(nr),
        "store_grade": str(nr.get("store_grade") or "").strip(),
        "days_since_last_sale": _row_days_last_selling(nr),
        "days_since_last_transfer": _days_since_transfer(nr),
        "last_week_sales": _pick_last_week_sold_qty(nr),
        "last_35day_sold_qty": _pick_sold_qty_last_35d(nr),
        "total_sales": _pick_int(nr, "sum_of_sales", "total_sales", "total_net_sales", default=0),
        "qty_still_needed": need,
        "recommended_qty_to_send": rec_send,
        "priority_score": _row_priority_score_value(nr) if _row_priority_score_value(nr) is not None else "",
    }


# Recommended Excel sheet: fixed column order only — values come straight from the lake row (no enrich / no joins).
RECOMMENDED_DOWNLOAD_COLUMNS: Tuple[str, ...] = (
    "receiver_site_name",
    "receiver_city",
    "receiver_state",
    "receiver_store_grade",
    "donor_site_name",
    "donor_city",
    "donor_state",
    "donor_store_grade",
    "sku",
    "size",
    "barcode",
    "required_qty",
    "transfer_qty",
    "receiver_soh",
    "donor_soh",
    "priority_score",
    "status",
    "created_at",
)

# When the lake row has no line-level qty fields, sum these stack columns (same names OneLake uses).
_REQ_QTY_STACK_KEYS: Tuple[str, ...] = (
    "req_1xs_qty",
    "req_2s_qty",
    "req_3m_qty",
    "req_4l_qty",
    "req_5xl_qty",
    "req_6xxl_qty",
)
_TRANSFER_QTY_STACK_KEYS: Tuple[str, ...] = (
    "transfer_1xs_qty",
    "transfer_1_xs_qty",
    "transfer_2s_qty",
    "transfer_3m_qty",
    "transfer_4l_qty",
    "transfer_5xl_qty",
    "transfer_6xxl_qty",
)
_REC_SOH_STACK_KEYS: Tuple[str, ...] = (
    "rec_soh_1xs",
    "rec_soh_1_xs",
    "rec_storesoh_1xs",
    "rec_storesoh_1_xs",
    "rec_soh_2s",
    "rec_storesoh_2s",
    "rec_soh_3m",
    "rec_storesoh_3m",
    "rec_soh_4l",
    "rec_storesoh_4l",
    "rec_soh_5xl",
    "rec_storesoh_5xl",
    "rec_soh_6xxl",
    "rec_soh_xxl",
    "rec_storesoh_6xxl",
    "rec_storesoh_xxl",
)
_DON_SOH_STACK_KEYS: Tuple[str, ...] = (
    "don_soh_1xs",
    "don_soh_1_xs",
    "don_storesoh_1xs",
    "don_storesoh_1_xs",
    "don_soh_2s",
    "don_storesoh_2s",
    "don_soh_3m",
    "don_storesoh_3m",
    "don_soh_4l",
    "don_storesoh_4l",
    "don_soh_5xl",
    "don_storesoh_5xl",
    "don_soh_6xxl",
    "don_soh_xxl",
    "don_storesoh_6xxl",
    "don_storesoh_xxl",
)


def _sum_int_keys(lc: Dict[str, Any], keys: Tuple[str, ...]) -> int:
    return sum(_safe_int(lc.get(k)) for k in keys)


def _recommended_sheet_row_from_lake(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    One Excel row per Delta row: same values as reading `RECOMMENDED_TRANSFERS_TABLE`, only reordered/aliased.
    Does not call _map_rec_row, enrich, or potential_receivers lookup (those changed numbers vs raw export).
    """
    safe = _json_safe_row(_dedupe_keys_case_insensitive_prefer_lower(dict(raw)))
    lc = {str(k).lower(): v for k, v in safe.items()}

    def gs(*keys: str) -> str:
        return _first_str_from_row(lc, *keys)

    req_line = _pick_int(
        lc,
        "required_qty",
        "receiver_recommended_refill",
        "recommended_refill",
        "receiver_required_refill",
        "recommendation_refill",
        "forecasted_refill",
        "refill",
    )
    req_stack = _sum_int_keys(lc, _REQ_QTY_STACK_KEYS)
    required_qty = req_line if req_line != 0 else req_stack

    tr_line = _pick_int(lc, "transfer_qty", "transfer_qty_total", "total_units")
    tr_stack = _sum_int_keys(lc, _TRANSFER_QTY_STACK_KEYS)
    transfer_qty = tr_line if tr_line != 0 else tr_stack

    wh_after_allocation: Any = ""
    for k in ("wh_soh_end_barcode", "warehouse_soh_after_allocation", "wh_soh_after_allocation"):
        if lc.get(k) not in (None, ""):
            wh_after_allocation = _safe_int(lc.get(k))
            break

    rs_line = _pick_int(lc, "receiver_soh", "receiver_store_soh", "receiver_lake_soh")
    rs_stack = _sum_int_keys(lc, _REC_SOH_STACK_KEYS)
    receiver_soh = rs_line if rs_line != 0 else rs_stack

    ds_line = _pick_int(lc, "donor_soh", "donor_store_soh", "don_soh", "donor_lake_soh")
    ds_stack = _sum_int_keys(lc, _DON_SOH_STACK_KEYS)
    donor_soh = ds_line if ds_line != 0 else ds_stack

    ps = lc.get("priority_score")
    ps_out: Any = ""
    if ps not in (None, ""):
        try:
            pf = float(ps)
            ps_out = round(pf, 4) if math.isfinite(pf) else str(ps)
        except (TypeError, ValueError):
            ps_out = str(ps)

    barcode = gs(
        "barcode",
        "rec_barcode",
        "receiver_barcode",
        "don_barcode",
        "donor_barcode",
        "receiver_lake_barcode",
    )

    return {
        "receiver_site_name": gs("receiver_site_name", "receiver_store_name"),
        "receiver_city": gs("receiver_city"),
        "receiver_state": gs("receiver_state"),
        "receiver_store_grade": gs("receiver_store_grade"),
        "donor_site_name": gs("donor_site_name", "donor_store_name"),
        "donor_city": gs("donor_city"),
        "donor_state": gs("donor_state"),
        "donor_store_grade": gs("donor_store_grade"),
        "sku": gs("sku"),
        "size": str(lc.get("size") or "").strip(),
        "barcode": barcode,
        "required_qty": required_qty,
        "transfer_qty": transfer_qty,
        "receiver_soh": receiver_soh,
        "donor_soh": donor_soh,
        "priority_score": ps_out,
        "status": gs("status"),
        "created_at": _excel_ts_display(lc.get("created_at")) or str(lc.get("created_at") or ""),
    }


@router.get("/download-transfer-details")
async def download_transfer_details(request: Request, max_rows: int = Query(50000, ge=200, le=50000)):
    """Download Excel: curated donor/receiver/in-transit; Recommended = lake rows as fixed-column projection (no transforms)."""
    try:
        # Export should be responsive for UI users; avoid forcing fresh OneLake reads for every sheet.
        # We already refresh table caches via sync/approve flows, so cached reads are usually current enough.
        bp = False
        # Potential lists: same tables + shapes as GET /data (Transfers UI grids).
        donor_rows = list(_read_transfer_table("potential_donors", bypass_cache=bp) or [])
        receiver_rows_all = list(_read_transfer_table("potential_receivers", bypass_cache=bp) or [])
        # Cap before heavy loops (slicing after building was a no-op and could time out / OOM).
        donor_rows = donor_rows[:max_rows]
        receiver_rows_all = receiver_rows_all[:max_rows]

        donors_data = []
        for r in donor_rows:
            nr = _normalize_row(_dedupe_keys_case_insensitive_prefer_lower(dict(r)))
            if not _row_classification_ok(nr, "donor"):
                continue
            donors_data.append(_potential_donor_export_row(r))

        receivers_data = []
        for r in receiver_rows_all:
            nr = _normalize_row(_dedupe_keys_case_insensitive_prefer_lower(dict(r)))
            if not _row_classification_ok(nr, "receiver"):
                continue
            if _row_refill_for_potential(nr) <= 0:
                continue
            receivers_data.append(_potential_receiver_export_row(r))

        # Recommended: one row per lake row; same underlying values as raw table read, fixed column order.
        rec_rows_raw = list(_read_transfer_table(RECOMMENDED_TRANSFERS_TABLE, bypass_cache=bp) or [])
        rec_rows_raw = rec_rows_raw[:max_rows]
        recommended_fixed = [_recommended_sheet_row_from_lake(r) for r in rec_rows_raw]
        df_recommended = pd.DataFrame(recommended_fixed, columns=list(RECOMMENDED_DOWNLOAD_COLUMNS))

        current_rows = list(_read_transfer_table("current_transfers", bypass_cache=bp) or [])
        current_rows = current_rows[:max_rows]

        df_donors = pd.DataFrame(donors_data)
        df_receivers = pd.DataFrame(receivers_data)
        try:
            for col in df_recommended.select_dtypes(include=["datetimetz"]).columns:
                df_recommended[col] = df_recommended[col].dt.tz_localize(None)
        except Exception:
            pass

        # --- Prepare Current Transfers DataFrame (same columns as Transfers UI > In transit tab only) ---
        df_current = _dataframe_current_transfers_in_transit_tab(current_rows or [])
        
        # --- Create Multi-Sheet Excel ---
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df_donors.to_excel(writer, index=False, sheet_name="Potential Donors")
            df_receivers.to_excel(writer, index=False, sheet_name="Potential Receivers")
            df_recommended.to_excel(writer, index=False, sheet_name="Recommended")
            df_current.to_excel(writer, index=False, sheet_name="In transit")

        filename = f"transfers_export_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        content = output.getvalue()
        log_download("Transfer Details", filename, request)
        return Response(
            content=content,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        logger.error(f"Generate consolidated Excel error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to generate consolidated Excel: {str(e)}")

@router.get("/sku-soh")
async def get_sku_soh(site: str, sku: str):
    """Fetch SOH for all sizes from OneLake snapshot (no SQL)."""
    try:
        source = _read_transfer_table(TRANSFER_SOURCE_TABLE)
        rows = []
        req_site = str(site or "").strip()
        req_sku = str(sku or "").strip().upper()
        for row in source:
            normalized = _normalize_row(row)
            row_site = str(normalized.get("site") or "").strip()
            row_site_id = str(normalized.get("site_id") or "").strip()
            row_site_name = str(normalized.get("site_name") or "").strip()
            row_sku = str(normalized.get("sku") or "").strip().upper()
            if row_sku != req_sku:
                continue
            # Site can arrive as code/id/name depending on recommendation row source.
            if req_site not in {row_site, row_site_id, row_site_name}:
                continue
            if row_sku == req_sku:
                rows.append(
                    {
                        "size": str(normalized.get("size") or ""),
                        "store_soh": _safe_qty(normalized.get("store_soh")),
                        "wh_soh": _safe_qty(normalized.get("wh_soh")),
                        "barcode": str(normalized.get("barcode") or ""),
                        "total_sales": _safe_int(normalized.get("sum_of_sales") or normalized.get("total_sales")),
                        "sold_qty_5w": _safe_int(normalized.get("sold_qty_prev_5w")),
                        "last_week_sales": _safe_int(normalized.get("lag1_sold_qty")),
                    }
                )
        data = rows
        return {"status": "success", "data": data}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/current")
async def get_current_transfers(
    donor_site: str = None,
    donor_sku: str = None,
    recv_site: str = None,
    recv_sku: str = None,
    refresh: int = Query(0, description="1 = skip in-memory cache, re-read OneLake (use after you cleared the table)."),
):
    try:
        rows_raw = _read_transfer_table("current_transfers", bypass_cache=(refresh == 1))
        
        # Apply mapping to all rows
        rows = [_map_rec_row(dict(r)) for r in rows_raw]
        
        # Apply filters
        filtered_rows = [
            r for r in rows
            if (not donor_site or donor_site in ("All Sites", "All Stores", "All") or str(r.get("donor_store") or "") == donor_site)
            and (not donor_sku or donor_sku in ("All SKUs", "All") or str(r.get("sku") or "") == donor_sku)
            and (not recv_site or recv_site in ("All Sites", "All Stores", "All") or str(r.get("receiver_store") or "") == recv_site)
            and (not recv_sku or recv_sku in ("All SKUs", "All") or str(r.get("sku") or "") == recv_sku)
        ]

        filtered_rows.sort(key=lambda x: str(x.get("created_at", "")), reverse=True)
        
        # Limit to 50 for UI performance
        display_rows = filtered_rows[:50]
        
        # Enrich locations
        try:
            replen_rows = _read_transfer_table(TRANSFER_SOURCE_TABLE)
            loc_idx = _replen_location_index(replen_rows) if replen_rows else {}
            site_name_idx = _site_name_index(replen_rows) if replen_rows else {}
        except Exception:
            loc_idx = {}
            site_name_idx = {}

        for r in display_rows:
            _enrich_recommendation_locations(r, loc_idx, site_name_idx)

        # Fix: ensure NaN/Inf values are JSON-compliant
        safe_rows = [_json_safe_row(r) for r in display_rows]
        
        return {"status": "success", "data": safe_rows}
    except Exception as e:
        logger.error(f"Error fetching current transfers: {e}")
        return {"status": "error", "message": str(e), "data": []}


def _prepare_zero_soh_full(
    site: str | None,
    max_days: int | None,
) -> Dict[str, Any]:
    """
    Load no-sales SOH dataset (default `ZERO_SOH_TABLE`), apply site + N-days filter, return JSON-safe rows and summary.
    Shared by /zero-soh and /zero-soh/export.
    """
    global _ZERO_SOH_CACHE, _ZERO_SOH_CACHE_AT, _ZERO_SOH_CACHE_SOURCE_TABLE
    now = time.time()
    cache_ok = (
        _ZERO_SOH_CACHE
        and _ZERO_SOH_CACHE_SOURCE_TABLE == ZERO_SOH_TABLE
        and (now - _ZERO_SOH_CACHE_AT) < _ZERO_SOH_CACHE_TTL_SECONDS
    )
    if cache_ok:
        rows = list(_ZERO_SOH_CACHE)
    else:
        rows = _read_transfer_table(ZERO_SOH_TABLE)
        _ZERO_SOH_CACHE = list(rows)
        _ZERO_SOH_CACHE_AT = now
        _ZERO_SOH_CACHE_SOURCE_TABLE = ZERO_SOH_TABLE
    if site and site not in ("All Sites", "All Stores", "All"):
        rows = [
            r for r in rows
            if str(r.get("Site") or r.get("Site_ID") or r.get("site") or r.get("site_id") or "") == site
        ]

    max_days_available = 0
    for r in rows:
        d = _to_int(r.get("days_since_last_transfer") or r.get("Days_Since_Last_Transfer"))
        if d > max_days_available:
            max_days_available = d

    no_transfer_rows = [_json_safe_row(r) for r in rows if _is_missing_transfer_date(r)]
    no_transfer_columns = list(no_transfer_rows[0].keys()) if no_transfer_rows else []

    if max_days is not None:
        filtered: List[Dict[str, Any]] = []
        for r in rows:
            if _is_missing_transfer_date(r):
                # Keep blank transfer-date records visible in filtered view as well.
                filtered.append(r)
                continue
            days = _to_float(r.get("days_since_last_transfer") or r.get("Days_Since_Last_Transfer"))
            if days > 0 and days <= max_days:
                filtered.append(r)
        rows = filtered

    rows = [_json_safe_row(r) for r in rows]
    summary = _build_zero_soh_summary(rows)
    return {
        "rows": rows,
        "summary": summary,
        "max_days_available": max_days_available,
        "no_transfer_rows": no_transfer_rows,
        "no_transfer_columns": no_transfer_columns,
    }


@router.get("/zero-soh")
async def get_zero_soh_data(
    site: str = None,
    limit: int = Query(300, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    max_days: int | None = Query(None, ge=0),
):
    """Fetch zero SOH dataset from OneLake Files snapshot."""
    try:
        prepared = _prepare_zero_soh_full(site, max_days)
        rows = prepared["rows"]
        total = len(rows)
        page_rows = rows[offset: offset + limit]
        columns = list(page_rows[0].keys()) if page_rows else (list(rows[0].keys()) if rows else [])
        return {
            "status": "success",
            "data": page_rows,
            "columns": columns,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": (offset + limit) < total,
            "summary": prepared["summary"],
            "max_days_available": prepared["max_days_available"],
            "no_transfer_record_count": len(prepared["no_transfer_rows"]),
            "no_transfer_records": prepared["no_transfer_rows"][:100],
            "no_transfer_columns": prepared["no_transfer_columns"],
        }
    except Exception as e:
        logger.error(f"Error fetching zero_sales_soh snapshot: {e}")
        return {
            "status": "error",
            "message": f"Failed to read OneLake zero_sales_soh snapshot. Error: {str(e)}",
            "version": "2.0.0-onelake",
            "data": [],
            "columns": []
        }


@router.get("/zero-soh/export")
async def export_zero_soh_excel(
    request: Request,
    site: str = None,
    max_days: int | None = Query(None, ge=0),
):
    """Download full No Sales Donors table as Excel; same filters as the UI (N days)."""
    try:
        prepared = _prepare_zero_soh_full(site, max_days)
        rows = prepared["rows"]
        if not rows:
            raise HTTPException(status_code=404, detail="No rows to export for the current filters.")
        df = pd.DataFrame(rows)
        output = BytesIO()
        sheet = "No_sales_donors"[:31]
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False, sheet_name=sheet)
        output.seek(0)
        filename = f"no_sales_donors_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        log_download("No Sales Donors (Zero SOH)", filename, request)
        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error exporting zero_sales_soh Excel: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
