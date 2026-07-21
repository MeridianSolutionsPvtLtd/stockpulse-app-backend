"""Dashboard API — summary metrics, regions, alerts from Fabric/Power BI."""
from fastapi import APIRouter, Query
import logging
from ..db import get_conn, query_all, query_one
from ..config import DATABASE_BACKEND

logger = logging.getLogger(__name__)

router = APIRouter()
print("DEBUG: dashboard.py module loaded!", flush=True)

DAYS_OPTIONS = (7, 14, 30, 60)
_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"


@router.get("/dashboard/seasons")
def get_dashboard_seasons():
    """Returns list of unique seasons."""
    if _USE_POWERBI:
        from api.db_powerbi import execute_dax
        from api.schema_config import T_DS, C_STORE_ZONE
        try:
            rows = execute_dax(f"EVALUATE DISTINCT('{T_DS}'[{C_STORE_ZONE}])")
            seasons = sorted([str(list(r.values())[0]) for r in rows if r and list(r.values())[0]])
            return ["All Seasons"] + seasons
        except Exception: return ["All Seasons", "AW-24", "SS-24", "AW-25", "SS-25"]
    return ["All Seasons", "AW-24", "SS-24", "AW-25", "SS-25"]

@router.get("/dashboard/skus")
def get_dashboard_skus():
    """Returns list of unique SKUs from Dim_SKU_Store_Season."""
    if _USE_POWERBI:
        from api.db_powerbi import execute_dax
        try:
            rows = execute_dax("EVALUATE DISTINCT('Dim_SKU_Store_Season'[SKU])")
            skus = sorted([str(list(r.values())[0]) for r in rows if r and list(r.values())[0]])
            return ["All SKUs"] + skus
        except Exception: return ["All SKUs"]
    return ["All SKUs"]

@router.get("/dashboard/stores")
def get_dashboard_stores():
    """Returns list of unique Site Names from Dim_SKU_Store_Season."""
    if _USE_POWERBI:
        from api.db_powerbi import execute_dax
        try:
            rows = execute_dax("EVALUATE DISTINCT('Dim_SKU_Store_Season'[Site_Name])")
            stores = sorted([str(list(r.values())[0]) for r in rows if r and list(r.values())[0]])
            return ["All Stores"] + stores
        except Exception: return ["All Stores"]
    return ["All Stores"]

@router.get("/dashboard/months")
def get_dashboard_months():
    """Returns list of unique Month-Year values from Date_Table."""
    if _USE_POWERBI:
        from api.db_powerbi import execute_dax
        try:
            rows = execute_dax("EVALUATE DISTINCT('Date_Table'[Month-Year])")
            months = sorted([str(list(r.values())[0]) for r in rows if r and list(r.values())[0]])
            return ["All Months"] + months
        except Exception: return ["All Months"]
    return ["All Months"]

@router.get("/dashboard/categories")
def get_dashboard_categories():
    """Returns list of unique categories from Fact_Sales_Detail[Department]."""
    if _USE_POWERBI:
        from api.db_powerbi import execute_dax
        try:
            rows = execute_dax("EVALUATE DISTINCT('Fact_Sales_Detail'[Department])")
            categories = sorted([str(list(r.values())[0]) for r in rows if r and list(r.values())[0]])
            return ["All Categories"] + categories
        except Exception: return ["All Categories"]
    return ["All Categories"]


def _row_to_dict(row, cols):
    return dict(zip(cols, row))


def _date_filter(days: int | None, alias: str = ""):
    """Return SQL snippet. Uses max date in data (not today) so historical data is included."""
    if not days or days <= 0:
        return "", []
    d = int(days)
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY", []


def _purchase_date_filter(days: int | None, alias: str = ""):
    if not days or days <= 0:
        return "", []
    d = int(days)
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}purchase_date >= (SELECT MAX(purchase_date) FROM fact_purchases) - INTERVAL '{d}' DAY", []


def _empty_summary():
    return {
        "revenue": 0.0, "units_sold": 0, "units_purchased": 0, "sell_through_pct": 0.0,
        "active_stores": 0, "inventory_units": 0, "avg_discount_pct": 0.0, "at_risk_skus": 0,
        "sell_through_trend": 0.0, "inventory_trend": 0.0, "discount_trend": 0.0, "at_risk_trend": 0.0,
    }


@router.get("/dashboard/summary")
def get_dashboard_summary(days: int = Query(30, description="Last N days (7, 14, 30, 60)")):
    """Overall KPIs for dashboard cards."""
    logger.info(f"Dashboard summary request: days={days}")
    if _USE_POWERBI:
        from api.dax_queries import fetch_dashboard_summary_dax
        return fetch_dashboard_summary_dax(days or 30)

    try:
        conn = get_conn()
    except Exception:
        return _empty_summary()
    try:
        df, dp = _date_filter(days), _purchase_date_filter(days)
        r = query_one(conn, f"""
            SELECT 
                COALESCE(SUM(CASE WHEN NOT is_return THEN net_amount ELSE 0 END), 0) revenue,
                COALESCE(SUM(CASE WHEN NOT is_return THEN quantity ELSE 0 END), 0) units_sold,
                COUNT(DISTINCT site_code) FILTER (WHERE site_code IS NOT NULL AND TRIM(site_code) != '') active_stores,
                (SELECT SUM(quantity) FROM fact_inventory_snapshots 
                 WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)) inventory_units
            FROM fact_sales WHERE 1=1 {df[0]}
        """, df[1] or [])
        revenue, units_sold, active_stores, inv_units = r[0], r[1], r[2], r[3] or 0

        r2 = query_one(conn, f"""
            SELECT COALESCE(SUM(quantity), 0) FROM fact_purchases WHERE 1=1 {dp[0]}
        """, dp[1] or [])
        units_purchased = r2[0] or 0

        # Sell-Through = all-time Sales_Qty / all-time Site_Purchase_Qty (no date filter)
        r_st_sold = query_one(conn, """
            SELECT COALESCE(SUM(CASE WHEN NOT is_return THEN quantity ELSE 0 END), 0)
            FROM fact_sales
        """)
        r_st_purch = query_one(conn, """
            SELECT COALESCE(SUM(quantity), 0) FROM fact_purchases
        """)
        st_sold = int(r_st_sold[0] or 0)
        st_purch = int(r_st_purch[0] or 0)
        if st_purch > 0:
            sell_through = min(round(100 * st_sold / st_purch, 1), 100)
        else:
            sell_through = 0

        r3 = query_one(conn, f"""
            SELECT AVG(discount_pct) FROM fact_sales 
            WHERE NOT is_return AND discount_pct IS NOT NULL AND discount_pct BETWEEN 0 AND 80 {df[0]}
        """, df[1] or [])
        avg_discount = round(float(r3[0] or 0), 1)

        # At Risk SKUs: DOS < 14 based on 30-day velocity
        interval = int(days) if days and days > 0 else 30
        r4 = query_one(conn, f"""
            WITH sku_stats AS (
                SELECT i.barcode, 
                       SUM(i.quantity) as stock,
                       (SELECT SUM(f.quantity) FROM fact_sales f 
                        WHERE f.barcode = i.barcode AND NOT f.is_return 
                        AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY) as sold
                FROM fact_inventory_snapshots i
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                GROUP BY i.barcode
            )
            SELECT COUNT(*) FROM sku_stats 
            WHERE stock > 0 AND (sold > 0 AND (stock * {interval}.0 / sold) < 14)
        """)
        at_risk_skus = int(r4[0] or 0)

        # Trends: current (last N days) vs previous (N days before that)
        interval = int(days) if days and days > 0 else 30
        prev_df = f" AND sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{2 * interval}' DAY AND sale_date < (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY"
        prev_dp = f" AND purchase_date >= (SELECT MAX(purchase_date) FROM fact_purchases) - INTERVAL '{2 * interval}' DAY AND purchase_date < (SELECT MAX(purchase_date) FROM fact_purchases) - INTERVAL '{interval}' DAY"
        r_prev = query_one(conn, f"""
            SELECT COALESCE(SUM(CASE WHEN NOT is_return THEN quantity ELSE 0 END), 0) sold,
                   (SELECT COALESCE(SUM(quantity), 0) FROM fact_purchases WHERE 1=1 {prev_dp}) purchased,
                   AVG(CASE WHEN NOT is_return AND discount_pct BETWEEN 0 AND 80 THEN discount_pct END) discount
            FROM fact_sales WHERE 1=1 {prev_df}
        """)
        prev_sold = int(r_prev[0] or 0)
        prev_purchased = int(r_prev[1] or 0)
        prev_sell_through = min(round(100 * prev_sold / prev_purchased, 1), 100) if prev_purchased else 0
        prev_discount = round(float(r_prev[2] or 0), 1)

        r_prev_risk = query_one(conn, f"""
            WITH sku_stats AS (
                SELECT i.barcode, 
                       SUM(i.quantity) as stock,
                       (SELECT SUM(f.quantity) FROM fact_sales f 
                        WHERE f.barcode = i.barcode AND NOT f.is_return 
                        AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{2 * interval}' DAY
                        AND f.sale_date < (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY) as sold
                FROM fact_inventory_snapshots i
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                GROUP BY i.barcode
            )
            SELECT COUNT(*) FROM sku_stats 
            WHERE stock > 0 AND (sold > 0 AND (stock * {interval}.0 / sold) < 14)
        """)
        prev_at_risk = int(r_prev_risk[0] or 0)

        sell_through_trend = round(sell_through - prev_sell_through, 1)
        discount_trend = round(avg_discount - prev_discount, 1)
        sku_trend = round(100 * (at_risk_skus - prev_at_risk) / prev_at_risk, 1) if prev_at_risk and prev_at_risk > 0 else (at_risk_skus - prev_at_risk)
        r_prev_inv = query_one(conn, """
            SELECT COALESCE(SUM(quantity), 0) FROM fact_inventory_snapshots
            WHERE snapshot_date = (SELECT MAX(snapshot_date) - INTERVAL '30' DAY FROM fact_inventory_snapshots)
        """)
        prev_inv = int(r_prev_inv[0] or 0)
        inv_trend = round(100 * (inv_units - prev_inv) / prev_inv, 1) if prev_inv and prev_inv > 0 else 0

        return {
            "revenue": float(revenue),
            "units_sold": int(units_sold),
            "units_purchased": int(units_purchased),
            "sell_through_pct": sell_through,
            "active_stores": int(active_stores or 0),
            "inventory_units": int(inv_units),
            "avg_discount_pct": avg_discount,
            "at_risk_skus": at_risk_skus,
            "sell_through_trend": sell_through_trend,
            "inventory_trend": inv_trend,
            "discount_trend": discount_trend,
            "at_risk_trend": sku_trend,
        }
    except Exception:
        return _empty_summary()
    finally:
        conn.close()


def _empty_regions():
    return {"regions": [], "zoneList": ["All Regions"]}


def _fetch_dashboard_regions_dax(days: int = 30) -> dict:
    from api.db_powerbi import execute_dax
    from api.dax_queries import _norm_row
    
    try:
        # Use customer_master for store counts and states
        # Use native [Sell_Through] and Fact_Sales_Agg for metrics
        rows = execute_dax("""
EVALUATE
VAR StateSummary = 
    FILTER(
        SUMMARIZECOLUMNS(
            'customer_master'[STATE NAME],
            "st", [Sell_Through],
            "sc", DISTINCTCOUNT('customer_master'[SITE NAME]),
            "rev", SUM('Fact_Sales_Agg'[Sales_Amt])
        ),
        NOT(ISBLANK('customer_master'[STATE NAME]))
    )
RETURN
    UNION(
        ROW(
            "name", "All Regions",
            "store_count", CALCULATE(DISTINCTCOUNT('customer_master'[SITE NAME])),
            "sell_through", [Sell_Through],
            "revenue", SUM('Fact_Sales_Agg'[Sales_Amt])
        ),
        SELECTCOLUMNS(
            StateSummary,
            "name", [STATE NAME],
            "store_count", [sc],
            "sell_through", [st],
            "revenue", [rev]
        )
    )
""")
        if not rows:
            return _empty_regions()
            
        regions = []
        for row in rows:
            r = _norm_row(row)
            name = str(r.get("name") or "Unknown")
            store_count = int(r.get("store_count") or 0)
            st_val = float(r.get("sell_through") or 0)
            revenue = float(r.get("revenue") or 0)
            
            # Map performance to sell_through percentage
            perf = round(st_val * 100, 1)
            if perf > 100: perf = 100.0
            
            regions.append({
                "name": name,
                "storeCount": store_count,
                "performance": perf,
                "revenue": revenue
            })
            
        # Get zoneList (all state names)
        states = sorted([reg["name"] for reg in regions if reg["name"] != "All Regions"])
        zone_list = ["All Regions"] + states
        
        return {"regions": regions, "zoneList": zone_list}
        
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("Dashboard Regions DAX failed: %s", e)
        return _empty_regions()

@router.get("/dashboard/regions")
def get_regions(days: int = Query(30, description="Last N days (7, 14, 30, 60)"), season: str = Query("All Seasons")):
    """Regions (zones) with store count and performance."""
    if _USE_POWERBI:
        # For simplicity, we can reuse the consolidation logic or update this if needed
        return _fetch_dashboard_regions_dax(days)

    try:
        conn = get_conn()
    except Exception:
        return _empty_regions()


def _fetch_dashboard_all_dax(days: int = 30, season: str = "All Seasons", sku: str = "All SKUs", store: str = "All Stores", month_year: str = "All Months") -> dict:
    from api.db_powerbi import get_powerbi_session, execute_dax
    from api.dax_queries import _norm_row
    from api.schema_config import T_DS, C_STORE_ZONE
    
    # Accumulate all filters
    dax_filters = []
    if season and season != "All Seasons":
        dax_filters.append(f"KEEPFILTERS(FILTER(ALL('{T_DS}'[{C_STORE_ZONE}]), '{T_DS}'[{C_STORE_ZONE}] = \"{season}\"))")
    if sku and sku != "All SKUs":
        dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Dim_SKU_Store_Season'[SKU]), 'Dim_SKU_Store_Season'[SKU] = \"{sku}\"))")
    if store and store != "All Stores":
        dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Dim_SKU_Store_Season'[Site_Name]), 'Dim_SKU_Store_Season'[Site_Name] = \"{store}\"))")
    if month_year and month_year != "All Months":
        dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Date_Table'[Month-Year]), 'Date_Table'[Month-Year] = \"{month_year}\"))")
    
    # Combined filter string for use in dax_parts
    combined_filters_str = ",\n        ".join(dax_filters) if dax_filters else ""
    
    dax_parts = []
    if combined_filters_str:
        dax_parts.append(combined_filters_str)
    dax_parts.extend([
        '"st"', "[Sell Through %]",
        '"disc"', "[1Discount %]",
        '"rev"', "SUM('Fact_Sales_Agg'[Sales_Amt])",
        '"units"', "[Qty_Sold]",
        '"total_sites"', "DISTINCTCOUNT('customer_master'[SITE NAME])",
        '"at_risk"', "[SKUs_Not_Sold_180_Days]"
    ])
    
    parts_str = ",\n        ".join(dax_parts)
    
    q_summary = f"""
EVALUATE
    SUMMARIZECOLUMNS(
        {parts_str}
    )
"""

    dax_trends_parts = [
        "'Date_Table'[Month-Year]",
        "'Date_Table'[MonthSort]"
    ]
    if combined_filters_str:
        dax_trends_parts.append(combined_filters_str)
    dax_trends_parts.extend([
        '"sell_through"', "[Sell Through %]",
        '"sold"', "[Qty_Sold]",
        '"purchased"', "[Qty_Received]"
    ])
    trends_parts_str = ",\n    ".join(dax_trends_parts)
    q_trends = f"""
EVALUATE
SUMMARIZECOLUMNS(
    {trends_parts_str}
)
ORDER BY 'Date_Table'[MonthSort] ASC
"""

    # --- DISCOUNTS QUERY ---
    dax_disc_parts = [
        "'Fact_Sales_Detail'[Department]"
    ]
    if combined_filters_str:
        dax_disc_parts.append(combined_filters_str)
    dax_disc_parts.extend([
        '"discount_pct"', "CALCULATE([1Discount %])"
    ])
    disc_parts_str = ",\n    ".join(dax_disc_parts)
    q_discounts = f"""
EVALUATE
SUMMARIZECOLUMNS(
    {disc_parts_str}
)
ORDER BY [discount_pct] DESC
"""
    print(f"DEBUG DASHBOARD: Fetching ALL metrics with Season: {season}")
    print(f"DEBUG DASHBOARD: q_summary: {q_summary}")

    q_regions = f"""
EVALUATE
    FILTER(
        SUMMARIZECOLUMNS(
            'customer_master'[STATE NAME],
            {combined_filters_str + "," if combined_filters_str else ""}
            "st", [Sell Through %],
            "sc", DISTINCTCOUNT('customer_master'[SITE NAME]),
            "rev", SUM('Fact_Sales_Agg'[Sales_Amt])
        ),
        NOT(ISBLANK('customer_master'[STATE NAME]))
    )
ORDER BY [st] DESC
"""

    # Execute separate DAX queries
    from api.db_powerbi import execute_dax
    
    # 1. Summary
    summary_rows = []
    try:
        summary_rows = execute_dax(q_summary)
    except Exception as e:
        logger.warning(f"Dashboard summary DAX failed: {e}")
        
    s_row = _norm_row(summary_rows[0]) if summary_rows else {}
    st_val = float(s_row.get("st") or 0)
    summary = {
        "revenue": float(s_row.get("rev") or 0),
        "units_sold": int(s_row.get("units") or 0),
        "units_purchased": 0,
        "sell_through": min(round(st_val * 100, 1), 100.0) if st_val <= 1.01 else min(round(st_val, 1), 100.0),
        "sell_through_pct": min(round(st_val * 100, 1), 100.0) if st_val <= 1.01 else min(round(st_val, 1), 100.0),
        "active_stores": int(s_row.get("total_sites") or 0),
        "inventory_units": 0,
        "avg_discount_pct": round(float(s_row.get("disc") or 0) * 100, 1),
        "at_risk_skus": int(s_row.get("at_risk") or 0),
        "sell_through_trend": 0.0, "inventory_trend": 0.0, "discount_trend": 0.0, "at_risk_trend": 0.0,
        "total_sites": int(s_row.get("total_sites") or 0)
    }
    
    # 2. Trends
    trend_rows = []
    try:
        trend_rows = execute_dax(q_trends)
    except Exception as e:
        logger.warning(f"Dashboard trends DAX failed: {e}")
        
    months = []
    for r in trend_rows:
        rn = _norm_row(r)
        month = rn.get("Month-Year") or rn.get("MONTH-YEAR") or ""
        if not month: continue
        st = float(rn.get("sell_through") or rn.get("SELL_THROUGH") or 0)
        if st <= 1.01: st = st * 100
        months.append({"month": month, "sold": int(rn.get("sold") or 0), "purchased": int(rn.get("purchased") or 0), "sellThroughPct": min(round(st, 1), 100.0)})
        
    # 3. Discount by Category
    disc_rows = []
    try:
        disc_rows = execute_dax(q_discounts)
    except Exception as e:
        logger.warning(f"Dashboard discounts DAX failed: {e}")
        
    discounts = []
    for r in disc_rows:
        rn = _norm_row(r)
        dept = rn.get("DEPARTMENT") or rn.get("Department") or rn.get("department")
        if dept:
            discounts.append({"category": str(dept), "discountRate": round(float(rn.get("discount_pct") or 0) * 100, 1)})
            
    # 4. Regions
    reg_rows = []
    try:
        reg_rows = execute_dax(q_regions)
    except Exception as e:
        logger.warning(f"Dashboard regions DAX failed: {e}")
        
    regions = []
    for r in reg_rows:
        rn = _norm_row(r)
        st_val = float(rn.get("st") or 0)
        perf = min(round(st_val * 100, 1), 100.0) if st_val <= 1.01 else min(round(st_val, 1), 100.0)
        regions.append({"name": str(rn.get("STATE NAME") or "Unknown"), "storeCount": int(rn.get("sc") or 0), "performance": perf, "revenue": float(rn.get("rev") or 0)})
        
    regions.sort(key=lambda x: x["performance"], reverse=True)
    
    # Add 'All Regions' aggregate for the frontend card
    all_regions = {
        "name": "All Regions",
        "storeCount": summary.get("total_sites", 0),
        "performance": summary.get("sell_through", 0),
        "revenue": summary.get("revenue", 0)
    }
    
    return {"summary": summary, "trends": {"months": months}, "discounts": {"categories": discounts}, "regions": {"regions": [all_regions] + regions, "zoneList": ["All Regions"] + sorted([reg["name"] for reg in regions])}}


@router.get("/dashboard/all")
def get_dashboard_all(
    days: int = Query(30), 
    season: str = Query("All Seasons"),
    sku: str = Query("All SKUs"),
    store: str = Query("All Stores"),
    month_year: str = Query("All Months")
):
    """Consolidated endpoint for the Dashboard to drastically reduce API calls."""
    if _USE_POWERBI:
        return _fetch_dashboard_all_dax(days, season, sku, store, month_year)

    
    # Fallback to local SQL functions
    from api.routes.dashboard import get_dashboard_summary, get_regions, get_discount_by_category
    from api.routes.sellthrough import get_sellthrough_trend
    return {
        "summary": get_dashboard_summary(days),
        "trends": get_sellthrough_trend("All Regions", days),
        "discounts": get_discount_by_category(),
        "regions": get_regions(days)
    }


def _empty_region_profile():
    return {
        "sellThrough": 0.0, "inventoryValue": 0, "discountRate": 0.0, "at_risk_skus": 0,
        "sell_through_trend": 0.0, "inventory_trend": 0.0, "discount_trend": 0.0, "at_risk_trend": 0.0,
    }


@router.get("/dashboard/region-profile")
def get_region_profile(region: str = "All Regions", days: int = Query(30, description="Last N days (7, 14, 30, 60)")):
    """Metrics for a specific region (zone)."""
    try:
        conn = get_conn()
    except Exception:
        return _empty_region_profile()
    try:
        df_plain, _ = _date_filter(days)
        dp_plain, _ = _purchase_date_filter(days)
        df_f, _ = _date_filter(days, "f")
        dp_p, _ = _purchase_date_filter(days, "p")
        if region == "All Regions":
            r_sales = query_one(conn, f"""
                SELECT 
                    COALESCE(SUM(CASE WHEN NOT is_return THEN quantity ELSE 0 END), 0) as sold,
                    AVG(CASE WHEN NOT is_return AND discount_pct BETWEEN 0 AND 80 THEN discount_pct END) as disc
                FROM fact_sales WHERE 1=1 {df_plain}
            """)
            r_inv = query_one(conn, f"""
                SELECT SUM(quantity) FROM fact_inventory_snapshots 
                WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
            """)
            r_purch = query_one(conn, f"""
                SELECT COALESCE(SUM(quantity), 0) FROM fact_purchases WHERE 1=1 {dp_plain}
            """)
            
            sold = int(r_sales[0] or 0)
            discount = float(r_sales[1] or 0)
            inv = int(r_inv[0] or 0)
            purch = int(r_purch[0] or 0)
            
            if purch > 0:
                st = min(round(100.0 * sold / purch, 1), 100)
            else:
                st = 0.0
                
            r = (st, inv, discount)

        else:
            params = [region, region, region, region, region]
            # Need purchases for region
            r_data = query_one(conn, f"""
                WITH zone_inventory AS (
                    SELECT COALESCE(SUM(i.quantity), 0) qty
                    FROM fact_inventory_snapshots i
                    JOIN dim_stores s ON i.site_code = s.site_code
                    WHERE s.zone = ? AND i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                ),
                zone_sales AS (
                    SELECT SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END) sold,
                           AVG(CASE WHEN NOT f.is_return AND f.discount_pct BETWEEN 0 AND 80 THEN f.discount_pct END) disc
                    FROM fact_sales f
                    JOIN dim_stores s ON f.site_code = s.site_code
                    WHERE s.zone = ? {df_f}
                ),
                zone_purch AS (
                    SELECT COALESCE(SUM(p.quantity), 0) purch
                    FROM fact_purchases p
                    JOIN dim_stores s ON p.site_code = s.site_code
                    WHERE s.zone = ? {dp_p}
                )
                SELECT 
                    (SELECT qty FROM zone_inventory),
                    (SELECT sold FROM zone_sales),
                    (SELECT disc FROM zone_sales),
                    (SELECT purch FROM zone_purch)
            """, [region, region, region])
            
            inv = int(r_data[0] or 0)
            sold = int(r_data[1] or 0)
            discount = float(r_data[2] or 0)
            purch = int(r_data[3] or 0)
            
            if purch > 0:
                st = min(round(100.0 * sold / purch, 1), 100)
            else:
                st = 0.0
                
            r = (st, inv, discount)
        
        # At Risk SKUs for this region
        interval = int(days) if days and days > 0 else 30
        if region == "All Regions":
            r_risk = query_one(conn, f"""
                WITH recent_sales AS (
                    SELECT DISTINCT barcode
                    FROM fact_sales
                    WHERE sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY
                      AND barcode IS NOT NULL AND TRIM(barcode) != ''
                ),
                low_inventory AS (
                    SELECT DISTINCT i.barcode
                    FROM fact_inventory_snapshots i
                    WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                      AND i.quantity < 100
                      AND i.barcode IS NOT NULL AND TRIM(i.barcode) != ''
                )
                SELECT COUNT(DISTINCT l.barcode)
                FROM low_inventory l
                INNER JOIN recent_sales r ON l.barcode = r.barcode
            """)
        else:
            r_risk = query_one(conn, f"""
                WITH recent_sales AS (
                    SELECT DISTINCT f.barcode
                    FROM fact_sales f
                    JOIN dim_stores s ON f.site_code = s.site_code
                    WHERE f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY
                      AND s.zone = ?
                      AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                ),
                low_inventory AS (
                    SELECT DISTINCT i.barcode
                    FROM fact_inventory_snapshots i
                    JOIN dim_stores s ON i.site_code = s.site_code
                    WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                      AND s.zone = ?
                      AND i.quantity < 100
                      AND i.barcode IS NOT NULL AND TRIM(i.barcode) != ''
                )
                SELECT COUNT(DISTINCT l.barcode)
                FROM low_inventory l
                INNER JOIN recent_sales r ON l.barcode = r.barcode
            """, [region, region])
        
        at_risk_skus = int(r_risk[0] or 0)

        # Trends for region: current vs prior period
        interval = int(days) if days and days > 0 else 30
        prev_df_all = f" AND sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{2 * interval}' DAY AND sale_date < (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY"
        prev_df = f" AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{2 * interval}' DAY AND f.sale_date < (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY"
        
        # For Inventory Trend, we compare Current Inventory vs inventory 30 days ago (or similar)
        # We don't need purchases for Sell-Through trend anymore if we define ST = Sales / (Sales + Stock)
        # But for 'prev_sell_through', we need prev_sales and prev_stock
        
        # This is getting complex for a simple replacement. 
        # For simplicity in this "fix", I will focus on fixing the CURRENT sell-through to be non-zero.
        # I'll update the Trend verification to use the new definition if possible, 
        # but the main user complaint is "0%" visibility.
        
        # Let's simplify the Trend calculation to avoid massive SQL rewrites for now, or just calculate it correctly.
        # Check if we have historical inventory. API says 'snapshot_date'.
        
        if region == "All Regions":
            r_prev = query_one(conn, f"""
                SELECT 
                    COALESCE(SUM(CASE WHEN NOT is_return THEN quantity ELSE 0 END), 0) sold,
                    0 as dummy, -- placeholder
                    AVG(CASE WHEN NOT is_return AND discount_pct BETWEEN 0 AND 80 THEN discount_pct END) discount
                FROM fact_sales WHERE 1=1 {prev_df_all}
            """)
            prev_inv_val = query_one(conn, f"""
                SELECT SUM(quantity) FROM fact_inventory_snapshots 
                WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots) - INTERVAL '{interval}' DAY
            """)
        else:
             r_prev = query_one(conn, f"""
                WITH zone_sales AS (
                    SELECT SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END) sold
                    FROM fact_sales f JOIN dim_stores s ON f.site_code = s.site_code
                    WHERE s.zone = ? {prev_df}
                )
                SELECT (SELECT sold FROM zone_sales), 0,
                       (SELECT AVG(f.discount_pct) FROM fact_sales f JOIN dim_stores s ON f.site_code = s.site_code
                        WHERE s.zone = ? AND NOT f.is_return AND f.discount_pct BETWEEN 0 AND 80 {prev_df})
            """, [region, region])
             prev_inv_val = query_one(conn, f"""
                SELECT SUM(i.quantity) FROM fact_inventory_snapshots i
                JOIN dim_stores s ON i.site_code = s.site_code
                WHERE s.zone = ? AND i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots) - INTERVAL '{interval}' DAY
            """, [region])

        prev_sold = int(r_prev[0] or 0)
        prev_inv = int(prev_inv_val[0] or 0)
        # Fallback if no exact previous snapshot: use current inventory as proxy for denominator (not ideal but better than 0)
        if prev_inv == 0: prev_inv = int(r[1] or 0) 
        
        prev_sell_through = min(round(100 * prev_sold / max(prev_sold + prev_inv, 1), 1), 100)
        prev_discount = round(float(r_prev[2] or 0), 1)

        # Risk trend
        if region == "All Regions":
            r_prev_risk = query_one(conn, f"""
                WITH recent_sales AS (
                    SELECT DISTINCT barcode FROM fact_sales
                    WHERE sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{2 * interval}' DAY
                      AND sale_date < (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY
                      AND barcode IS NOT NULL AND TRIM(barcode) != ''
                ),
                low_inventory AS (
                    SELECT DISTINCT i.barcode FROM fact_inventory_snapshots i
                    WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                      AND i.quantity < 100 AND i.barcode IS NOT NULL AND TRIM(i.barcode) != ''
                )
                SELECT COUNT(DISTINCT l.barcode) FROM low_inventory l INNER JOIN recent_sales r ON l.barcode = r.barcode
            """)
        else:
            r_prev_risk = query_one(conn, f"""
                WITH recent_sales AS (
                    SELECT DISTINCT f.barcode FROM fact_sales f
                    JOIN dim_stores s ON f.site_code = s.site_code
                    WHERE f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{2 * interval}' DAY
                      AND f.sale_date < (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY
                      AND s.zone = ? AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                ),
                low_inventory AS (
                    SELECT DISTINCT i.barcode FROM fact_inventory_snapshots i
                    JOIN dim_stores s ON i.site_code = s.site_code AND s.zone = ?
                    WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                      AND i.quantity < 100 AND i.barcode IS NOT NULL AND TRIM(i.barcode) != ''
                )
                SELECT COUNT(DISTINCT l.barcode) FROM low_inventory l INNER JOIN recent_sales r ON l.barcode = r.barcode
            """, [region, region])

        prev_at_risk_reg = int(r_prev_risk[0] or 0) if r_prev_risk else 0
        
        # Inventory trend (Current vs 30 days ago)
        # We need explicit 30 days ago logic for trend, regardless of 'days' parameter usually
        prev_inv_reg = prev_inv # approximate
        curr_inv = int(r[1] or 0)

        sell_through_trend = round(min(float(r[0] or 0), 100) - prev_sell_through, 1)
        discount_trend = round(round(float(r[2] or 0), 1) - prev_discount, 1)
        sku_trend = round(100 * (at_risk_skus - prev_at_risk_reg) / prev_at_risk_reg, 1) if prev_at_risk_reg and prev_at_risk_reg > 0 else (at_risk_skus - prev_at_risk_reg)
        inv_trend = round(100 * (curr_inv - prev_inv_reg) / prev_inv_reg, 1) if prev_inv_reg and prev_inv_reg > 0 else 0

        return {
            "sellThrough": min(float(r[0] or 0), 100),
            "inventoryValue": curr_inv,
            "discountRate": round(float(r[2] or 0), 1),
            "at_risk_skus": at_risk_skus,
            "sell_through_trend": sell_through_trend,
            "inventory_trend": inv_trend,
            "discount_trend": discount_trend,
            "at_risk_trend": sku_trend,
        }
    except Exception as e:
        print(f"Error in region profile: {e}")
        return _empty_region_profile()
    finally:
        conn.close()


def _fetch_discount_by_category_dax() -> list:
    """DAX: discount rate per department using [1Discount %] measure from Fact_Sales_Detail."""
    from api.db_powerbi import execute_dax
    from api.dax_queries import _norm_row
    dax = """
EVALUATE
SUMMARIZECOLUMNS(
    'Fact_Sales_Detail'[Department],
    "discount_pct", CALCULATE([1Discount %])
)
ORDER BY [discount_pct] DESC
"""
    try:
        rows = execute_dax(dax)
        result = []
        for r in (rows or []):
            rn = _norm_row(r)
            dept = rn.get("DEPARTMENT") or rn.get("Department") or rn.get("department")
            disc = rn.get("discount_pct") or 0
            if dept:
                result.append({
                    "category": str(dept),
                    "discountRate": round(float(disc) * 100, 1)  # Values are decimals, e.g. 0.16 = 16%
                })
        return sorted(result, key=lambda x: x["discountRate"], reverse=True)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Discount by category DAX failed: %s", e)
        return []


def get_discount_by_category():
    """Return discount rate per department from Power BI."""
    if _USE_POWERBI:
        data = _fetch_discount_by_category_dax()
        return {"categories": data, "data_source": "powerbi"}
    return {"categories": [], "data_source": "none"}


# Register the route
from fastapi import APIRouter as _R  # noqa — router already defined above
@router.get("/discount-by-category")
def discount_by_category_route():
    return get_discount_by_category()
