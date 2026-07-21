"""Analytics API — unique, actionable business intelligence from DuckDB.

Focuses on insights NOT available on other pages:
- Discount effectiveness & pricing
- Size curve analysis
- Return rate analysis
- Revenue concentration & margin
"""
from fastapi import APIRouter, Query
from api.db import get_conn, query_all

router = APIRouter()
@router.get("/analytics/insights")
def get_analytics_insights(region: str = Query("All Regions"), days: int = Query(30)):
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, C_SALE_DATE, C_SALE_AMOUNT, C_SALE_IS_RETURN, C_SALE_QTY, C_SALE_DISCOUNT, T_FI, C_INV_QTY, C_INV_BARCODE, T_DP, C_PROD_DEPT, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    d = max(int(days), 1)
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    
    # Regional filter logic
    zone_filter = ""
    if region != "All Regions":
        zone_filter = f"FILTER(ALL('{T_DS}'), '{T_DS}'[{C_STORE_ZONE}]=\"{region}\")"
    
    try:
        # ── 1. DISCOUNT EFFECTIVENESS & OVERALL ──
        q_disc = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
VAR Filtered = FILTER('{T_FS}', {not_return} && '{T_FS}'[{C_SALE_DATE}] >= MaxDate - {d})
VAR WithFilter = {"CALCULATETABLE(Filtered, " + zone_filter + ")" if zone_filter else "Filtered"}
VAR WithBracket = ADDCOLUMNS(
    WithFilter,
    "bracket", SWITCH(TRUE(), 
        ['{C_SALE_DISCOUNT}'] <= 0, "Full Price",
        ['{C_SALE_DISCOUNT}'] <= 0.2, "1-20%",
        ['{C_SALE_DISCOUNT}'] <= 0.4, "21-40%",
        ['{C_SALE_DISCOUNT}'] <= 0.6, "41-60%",
        "61%+"
    )
)
RETURN SUMMARIZE(WithBracket, [bracket], 
    "units", SUM(['{C_SALE_QTY}']),
    "revenue", SUM(['{C_SALE_AMOUNT}']),
    "mrp_val", SUMX(WithBracket, ['{C_SALE_QTY}'] * RELATED('{T_DP}'[MRP])),
    "asp", AVERAGEX(WithBracket, ['{C_SALE_AMOUNT}'] / ['{C_SALE_QTY}']),
    "avg_mrp", AVERAGEX(WithBracket, RELATED('{T_DP}'[MRP]))
)
"""
        disc_rows = execute_dax(q_disc)
        total_rev = sum(float(r.get("revenue") or 0) for r in (disc_rows or []))
        total_mrp_val = sum(float(r.get("mrp_val") or 0) for r in (disc_rows or []))
        
        discount_brackets = []
        for r in (disc_rows or []):
            rev = float(r.get("revenue") or 0)
            asp = float(r.get("asp") or 0)
            avg_mrp = float(r.get("avg_mrp") or 1)
            discount_brackets.append({
                "bracket": r.get("bracket") or "Unknown",
                "units": int(r.get("units") or 0),
                "revenue": rev,
                "revenuePct": round(100 * rev / max(total_rev, 1), 1),
                "avgSellingPrice": round(asp),
                "avgMrp": round(avg_mrp),
                "realization": round(100 * asp / max(avg_mrp, 1), 1),
            })

        # ── 2. DISCOUNT BY CATEGORY (Sell-through comparison) ──
        q_cat = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
VAR S = SUMMARIZECOLUMNS(
    '{T_DP}'[{C_PROD_DEPT}],
    {zone_filter if zone_filter else "FILTER(ALL('" + T_FS + "'), " + not_return + " && '" + T_FS + "'[" + C_SALE_DATE + "] >= MaxDate - " + str(d) + ")"},
    "sold", SUM('{T_FS}'[{C_SALE_QTY}]),
    "revenue", SUM('{T_FS}'[{C_SALE_AMOUNT}]),
    "mrp_val", SUMX('{T_FS}', '{T_FS}'[{C_SALE_QTY}] * RELATED('{T_DP}'[MRP])),
    "avg_disc", AVERAGE('{T_FS}'[{C_SALE_DISCOUNT}])
)
VAR P = SUMMARIZECOLUMNS('{T_DP}'[{C_PROD_DEPT}], "purchased", SUM('{T_FI}'[{C_INV_QTY}]))
RETURN GENERATEALL(S, VAR c = '{T_DP}'[{C_PROD_DEPT}] RETURN FILTER(P, '{T_DP}'[{C_PROD_DEPT}] = c))
"""
        cat_rows = execute_dax(q_cat)
        discount_by_category = []
        for r in (cat_rows or []):
            rn = _norm_row(r)
            cat = rn.get(C_PROD_DEPT) or "Unclassified"
            sold = int(rn.get("sold") or 0)
            purchased = int(rn.get("purchased") or 1)
            st = min(round(100 * sold / purchased, 1), 100) if purchased > 0 else 0
            rev = float(rn.get("revenue") or 0)
            mrp_val = float(rn.get("mrp_val") or 0)
            discount_by_category.append({
                "category": cat,
                "avgDiscount": round(float(rn.get("avg_disc") or 0) * 100, 1),
                "sellThrough": st,
                "revenue": rev,
                "realization": round(100 * rev / max(mrp_val, 1), 1) if mrp_val > 0 else 0,
                "marginLoss": max(mrp_val - rev, 0),
                "unitsSold": sold,
            })

        # ── 3. SIZE CURVE ANALYSIS ──
        q_size = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
RETURN
SUMMARIZECOLUMNS(
    '{T_DP}'[Size],
    {zone_filter if zone_filter else "FILTER(ALL('" + T_FS + "'), " + not_return + " && '" + T_FS + "'[" + C_SALE_DATE + "] >= MaxDate - " + str(d) + ")"},
    "sold", SUM('{T_FS}'[{C_SALE_QTY}]),
    "revenue", SUM('{T_FS}'[{C_SALE_AMOUNT}]),
    "stock", SUM('{T_FI}'[{C_INV_QTY}])
)
"""
        size_rows = execute_dax(q_size)
        size_data = []
        for r in (size_rows or []):
            rn = _norm_row(r)
            s_units = int(rn.get("sold") or 0)
            s_stock = int(rn.get("stock") or 0)
            size_data.append({
                "size": rn.get("Size") or "Unknown",
                "stock": s_stock,
                "sold": s_units,
                "revenue": float(rn.get("revenue") or 0),
                "daysOfSupply": round(s_stock * d / max(s_units, 1)) if s_units > 0 else 999,
                "sellThrough": min(round(100 * s_units / max(s_stock, 1), 1), 100) if s_stock > 0 else 0,
            })
        size_data.sort(key=lambda x: -x["sold"])

        # ── 4. RETURN ANALYSIS ──
        q_ret = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
VAR Base = {"CALCULATETABLE('" + T_FS + "', " + zone_filter + ")" if zone_filter else "'" + T_FS + "'"}
VAR Agg = SUMMARIZECOLUMNS(
    "ret_units", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), '{T_FS}'[{C_SALE_IS_RETURN}] = TRUE() && '{T_FS}'[{C_SALE_DATE}] >= MaxDate - {d}),
    "sale_units", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), '{T_FS}'[{C_SALE_IS_RETURN}] = FALSE() && '{T_FS}'[{C_SALE_DATE}] >= MaxDate - {d}),
    "ret_val", CALCULATE(SUM('{T_FS}'[{C_SALE_AMOUNT}]), '{T_FS}'[{C_SALE_IS_RETURN}] = TRUE() && '{T_FS}'[{C_SALE_DATE}] >= MaxDate - {d}),
    "sale_val", CALCULATE(SUM('{T_FS}'[{C_SALE_AMOUNT}]), '{T_FS}'[{C_SALE_IS_RETURN}] = FALSE() && '{T_FS}'[{C_SALE_DATE}] >= MaxDate - {d})
)
RETURN Agg
"""
        ret_rows = execute_dax(q_ret)
        rr = _norm_row(ret_rows[0]) if ret_rows else {}
        ret_units = abs(int(rr.get("ret_units") or 0))
        sale_units = int(rr.get("sale_units") or 0)
        ret_val = abs(float(rr.get("ret_val") or 0))
        sale_val = float(rr.get("sale_val") or 0)
        
        return_overall = {
            "returnUnits": ret_units,
            "saleUnits": sale_units,
            "returnRate": round(100 * ret_units / max(sale_units, 1), 1),
            "returnValue": ret_val,
            "saleValue": sale_val,
            "valueRate": round(100 * ret_val / max(sale_val, 1), 1),
        }

        # ── 5. REVENUE CONCENTRATION (Top SKUs) ──
        q_top = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
RETURN
TOPN(15,
    SUMMARIZECOLUMNS(
        '{T_DP}'[SKU],
        '{T_DP}'[Barcode],
        '{T_DP}'[{C_PROD_DEPT}],
        '{T_DP}'[Size],
        {zone_filter if zone_filter else "FILTER(ALL('" + T_FS + "'), " + not_return + " && '" + T_FS + "'[" + C_SALE_DATE + "] >= MaxDate - " + str(d) + ")"},
        "sold", SUM('{T_FS}'[{C_SALE_QTY}]),
        "revenue", SUM('{T_FS}'[{C_SALE_AMOUNT}]),
        "mrp", AVERAGE('{T_DP}'[MRP])
    ),
    [revenue], DESC
)
"""
        top_rows = execute_dax(q_top)
        top_skus = []
        running_rev = 0
        for r in (top_rows or []):
            rn = _norm_row(r)
            rev = float(rn.get("revenue") or 0)
            running_rev += rev
            asp = rev / int(rn.get("sold") or 1)
            avg_mrp = float(rn.get("mrp") or 1)
            top_skus.append({
                "barcode": rn.get("Barcode") or "SKU",
                "sku": rn.get("SKU") or "SKU",
                "category": rn.get(C_PROD_DEPT) or "Unclassified",
                "size": rn.get("Size") or "N/A",
                "unitsSold": int(rn.get("sold") or 0),
                "revenue": rev,
                "asp": round(asp),
                "mrp": round(avg_mrp),
                "realization": round(100 * asp / max(avg_mrp, 1), 1),
                "cumulativeRevPct": round(100 * running_rev / max(total_rev, 1), 1),
            })

        # Season Mix
        q_season = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
RETURN
SUMMARIZECOLUMNS(
    '{T_DP}'[Season],
    {zone_filter if zone_filter else "FILTER(ALL('" + T_FS + "'), " + not_return + " && '" + T_FS + "'[" + C_SALE_DATE + "] >= MaxDate - " + str(d) + ")"},
    "sold", SUM('{T_FS}'[{C_SALE_QTY}]),
    "revenue", SUM('{T_FS}'[{C_SALE_AMOUNT}]),
    "avg_disc", AVERAGE('{T_FS}'[{C_SALE_DISCOUNT}])
)
"""
        season_rows = execute_dax(q_season)
        season_total_rev = sum(float(r.get("revenue") or 0) for r in (season_rows or []))
        season_mix = [{
            "season": r.get("Season") or "Unknown",
            "unitsSold": int(r.get("sold") or 0),
            "revenue": float(r.get("revenue") or 0),
            "revenuePct": round(100 * float(r.get("revenue") or 0) / max(season_total_rev, 1), 1),
            "avgDiscount": round(float(r.get("avg_disc") or 0) * 100, 1),
        } for r in (season_rows or [])]

        return {
            "discountAnalysis": {
                "brackets": discount_brackets,
                "byCategory": discount_by_category,
                "overallRealization": round(100 * total_rev / max(total_mrp_val, 1), 1),
                "totalRevenue": total_rev,
                "totalMrpValue": total_mrp_val,
                "marginLoss": max(total_mrp_val - total_rev, 0),
            },
            "sizeAnalysis": size_data,
            "returnAnalysis": {
                "overall": return_overall,
                "byCategory": [], # Optional enhancement
                "byRegion": [],
            },
            "revenueInsights": {
                "topSkus": top_skus,
                "revenuePerUnit": sorted([{"category": c["category"], "revenuePerUnit": round(c["revenue"] / max(c["unitsSold"], 1))} for c in discount_by_category], key=lambda x: -x["revenuePerUnit"]),
            },
            "seasonMix": season_mix,
        }
    except Exception as e:
        import logging; logging.getLogger(__name__).exception("Analytics Insights DAX failed: %s", e)
        return {"discountAnalysis": {"brackets": [], "byCategory": []}, "sizeAnalysis": [], "returnAnalysis": {"overall": {}}, "revenueInsights": {"topSkus": []}, "seasonMix": []}


@router.get("/analytics/trend")
def get_analytics_trend(region: str = Query("All Regions"), days: int = Query(30)):
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, C_SALE_DATE, C_SALE_AMOUNT, C_SALE_IS_RETURN, C_SALE_QTY, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax
    
    d = max(int(days), 1)
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    
    zone_filter = ""
    if region != "All Regions":
        zone_filter = f"FILTER(ALL('{T_DS}'), '{T_DS}'[{C_STORE_ZONE}]=\"{region}\")"

    try:
        query = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
VAR Filtered = FILTER('{T_FS}', {not_return} && '{T_FS}'[{C_SALE_DATE}] >= MaxDate - {d})
VAR WithFilter = {"CALCULATETABLE(Filtered, " + zone_filter + ")" if zone_filter else "Filtered"}
VAR WithMonth = ADDCOLUMNS(WithFilter, "m", FORMAT('{T_FS}'[{C_SALE_DATE}], "yyyy-MM"))
RETURN SUMMARIZE(WithMonth, [m], 
    "revenue", SUM('{T_FS}'[{C_SALE_AMOUNT}]),
    "units", SUM('{T_FS}'[{C_SALE_QTY}])
)
ORDER BY [m]
"""
        rows = execute_dax(query)
        return {"months": [{"month": r.get("m") or "Unknown", "revenue": float(r.get("revenue") or 0), "units": int(r.get("units") or 0)} for r in (rows or [])]}
    except Exception as e:
        import logging; logging.getLogger(__name__).exception("Analytics Trend DAX failed: %s", e)
        return {"months": []}
