from fastapi import APIRouter, Query
from api.db import get_conn, query_all
import pandas as pd

from api.config import DATABASE_BACKEND

router = APIRouter()
_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"

def _fetch_demand_signal_dax(region: str, season: str, limit: int) -> dict:
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, C_SALE_DATE, C_SALE_IS_RETURN, C_SALE_QTY, C_SALE_BARCODE, C_SALE_DISCOUNT, C_SALE_SITE, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    zone_filter = ""
    if region != "All Regions":
         zone_filter = f"FILTER(ALL('{T_DS}'), '{T_DS}'[{C_STORE_ZONE}]=\"{region}\")"

    try:
        # 1. Demand Curve Data
        curve_query = f"""
EVALUATE
VAR Base = CALCULATETABLE('{T_FS}', {not_return}, {zone_filter if zone_filter else "ALL('{T_FS}')"})
VAR SkuVel = SUMMARIZE(Base, '{T_FS}'[{C_SALE_BARCODE}], 
    "is_discount", IF(AVERAGE('{T_FS}'[{C_SALE_DISCOUNT}]) >= 0.1, "Discount", "Organic"),
    "vel", SUM('{T_FS}'[{C_SALE_QTY}]) / 30.0
)
VAR Buckets = ADDCOLUMNS(SkuVel, "bucket", 
    SWITCH(TRUE(), [vel] < 0.1, "0.0 - 0.1", [vel] < 0.5, "0.1 - 0.5", [vel] < 1.0, "0.5 - 1.0", [vel] < 3.0, "1.0 - 3.0", "3.0+")
)
RETURN SUMMARIZE(Buckets, [bucket], [is_discount], "sku_count", COUNTROWS(Buckets))
"""
        rows = execute_dax(curve_query)
        curve_map = {}
        for r in (rows or []):
            rn = _norm_row(r)
            b = rn.get("bucket") or "Unknown"
            t = rn.get("is_discount") or "Organic"
            if b not in curve_map: curve_map[b] = {"velocity_bucket": b, "Organic": 0, "Discount": 0}
            curve_map[b][t] = int(rn.get("sku_count") or 0)
        
        # 2. Heatmap Data
        heatmap_query = f"""
EVALUATE
TOPN({limit},
    SUMMARIZECOLUMNS(
        'dim_stores'[Store Type],
        'dim_products'[Department],
        {zone_filter if zone_filter else ""},
        "org_qty", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), '{T_FS}'[{C_SALE_DISCOUNT}] < 0.1, {not_return}),
        "disc_qty", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), '{T_FS}'[{C_SALE_DISCOUNT}] >= 0.1, {not_return}),
        "tot_qty", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), {not_return})
    ),
    [tot_qty], DESC
)
"""
        hrows = execute_dax(heatmap_query)
        hdata = []
        for r in (hrows or []):
            rn = _norm_row(r)
            t = float(rn.get("tot_qty") or 1)
            d = float(rn.get("disc_qty") or 0)
            hdata.append({
                "format": rn.get("Store Type") or "Standard",
                "category": rn.get("Department") or "General",
                "organic_qty": int(rn.get("org_qty") or 0),
                "discount_qty": int(d),
                "total_qty": int(t),
                "discount_dependency": round(d/t, 2)
            })
            
        return {"demand_curve": list(curve_map.values()), "heatmap_data": hdata}
    except Exception as e:
        import logging; logging.getLogger(__name__).error("Demand Signal DAX failed: %s", e)
        return {"demand_curve": [], "heatmap_data": []}

@router.get("/demand-signal")
def get_demand_signal(
    region: str = Query(None, description="Filter by Region (Zone)"),
    season: str = Query(None, description="Filter by Season"),
    limit: int = Query(50)
):
    """
    Extracts demand signal by separating Organic vs Discount-driven sales.
    """
    if _USE_POWERBI:
        return _fetch_demand_signal_dax(region, season, limit)
        
    try:
        conn = get_conn()
    except Exception:
        return {"error": "Database connection failed"}

    try:
        filters = []
        params = []
        
        if region and region != "All Regions":
            filters.append("s.zone = ?")
            params.append(region)
            
        where_clause = " AND ".join(filters)
        if where_clause:
            where_clause = f"AND {where_clause}"

        # 1. Demand Curve Data: Histogram of SKU velocities (Organic vs Discount)
        # Calculate daily velocity per SKU/Store, then bucket them.
        
        # We look at last 90 days for velocity calculation
        sql_curve = f"""
        WITH sales_base AS (
            SELECT 
                f.barcode,
                p.department,
                s.store_type,
                f.quantity,
                CASE 
                    WHEN (f.discount_pct >= 10.0) THEN 'Discount' 
                    ELSE 'Organic' 
                END as sale_type
            FROM fact_sales f
            JOIN dim_products p ON f.barcode = p.barcode
            JOIN dim_stores s ON f.site_code = s.site_code
            WHERE f.sale_date >= (CURRENT_DATE - INTERVAL 30 DAY)
            AND f.quantity > 0
            AND f.is_return = FALSE
            {where_clause}
        ),
        sku_velocity AS (
            SELECT 
                barcode,
                sale_type,
                SUM(quantity) / 30.0 as daily_velocity
            FROM sales_base
            GROUP BY barcode, sale_type
        ),
        buckets AS (
            SELECT 
                barcode,
                sale_type,
                CASE 
                    WHEN daily_velocity < 0.1 THEN '0.0 - 0.1'
                    WHEN daily_velocity < 0.5 THEN '0.1 - 0.5'
                    WHEN daily_velocity < 1.0 THEN '0.5 - 1.0'
                    WHEN daily_velocity < 3.0 THEN '1.0 - 3.0'
                    ELSE '3.0+' 
                END as velocity_bucket,
                daily_velocity
            FROM sku_velocity
        )
        SELECT 
            velocity_bucket,
            sale_type,
            COUNT(DISTINCT barcode) as sku_count,
            SUM(daily_velocity) as total_velocity
        FROM buckets
        GROUP BY velocity_bucket, sale_type
        ORDER BY velocity_bucket
        """
        
        curve_rows = query_all(conn, sql_curve, params)
        
        # Transform for frontend:
        # { bucket: "0.1-0.5", organic: 120, discount: 45 }
        curve_map = {}
        for r in curve_rows:
            bucket = r[0]
            sType = r[1]
            count = r[2]
            
            if bucket not in curve_map:
                curve_map[bucket] = {"velocity_bucket": bucket, "Organic": 0, "Discount": 0}
            curve_map[bucket][sType] = count
            
        demand_curve = list(curve_map.values())
        # Sort buckets logically if needed (simple alphanumeric sort works for these specific buckets mostly, except 10+)
        # We can enforce order in frontend or list.

        # 2. Heatmap Data: Store Format vs Category correlation with Discount Dependency
        # Discount Dependency = Discount Sales Qty / Total Sales Qty
        
        sql_heatmap = f"""
        SELECT 
            s.store_type as store_format,
            p.department as category,
            SUM(CASE WHEN (f.discount_pct < 10.0) THEN f.quantity ELSE 0 END) as organic_qty,
            SUM(CASE WHEN (f.discount_pct >= 10.0) THEN f.quantity ELSE 0 END) as discount_qty,
            SUM(f.quantity) as total_qty
        FROM fact_sales f
        JOIN dim_products p ON f.barcode = p.barcode
        JOIN dim_stores s ON f.site_code = s.site_code
        WHERE f.sale_date >= (CURRENT_DATE - INTERVAL 30 DAY)
        AND f.quantity > 0 -- Exclude returns for demand signal
        AND f.is_return = FALSE
        {where_clause}
        GROUP BY s.store_type, p.department
        HAVING total_qty > 100
        ORDER BY total_qty DESC
        LIMIT {limit}
        """
        
        heatmap_rows = query_all(conn, sql_heatmap, params)
        heatmap_data = []
        for r in heatmap_rows:
            org = r[2] or 0
            disc = r[3] or 0
            tot = r[4] or 1
            dependency = round(disc / tot, 2)
            heatmap_data.append({
                "format": r[0],
                "category": r[1],
                "organic_qty": org,
                "discount_qty": disc,
                "total_qty": tot,
                "discount_dependency": dependency
            })

        return {
            "demand_curve": demand_curve,
            "heatmap_data": heatmap_data
        }

    except Exception as e:
        print(f"Error in demand-signal: {e}")
        return {"error": str(e), "demand_curve": [], "heatmap_data": []}
    finally:
        conn.close()
