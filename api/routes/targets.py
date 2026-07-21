"""Targets API — store targets and actual vs target performance."""
from fastapi import APIRouter, Query
from api.db import get_conn, query_all, query_one

router = APIRouter()


@router.get("/targets/summary")
def get_targets_summary(period: str = Query("2026-02")):
    """
    Overall target vs actual for a period.
    Period format: YYYY-MM
    """
    conn = get_conn()
    try:
        # Get target summary
        targets = query_one(conn, """
            SELECT 
                target_period,
                SUM(target_amount) as total_target,
                COUNT(*) as store_count,
                COUNT(DISTINCT zone) as zone_count
            FROM dim_store_targets
            WHERE target_period = ?
            GROUP BY target_period
        """, [period])
        
        if not targets or targets[0] is None:
            return {"period": period, "totalTarget": 0, "totalActual": 0, "achievement": 0, "stores": 0}
        
        # Get actual sales for the period
        year, month = period.split('-')
        actual = query_one(conn, """
            SELECT COALESCE(SUM(net_amount), 0)
            FROM fact_sales
            WHERE NOT is_return
              AND EXTRACT(YEAR FROM sale_date) = ?
              AND EXTRACT(MONTH FROM sale_date) = ?
        """, [int(year), int(month)])
        
        total_target = float(targets[1] or 0)
        total_actual = float(actual[0] or 0)
        achievement = round(100 * total_actual / total_target, 1) if total_target else 0
        
        return {
            "period": period,
            "totalTarget": total_target,
            "totalActual": total_actual,
            "achievement": achievement,
            "stores": int(targets[2] or 0),
            "zones": int(targets[3] or 0)
        }
    finally:
        conn.close()


@router.get("/targets/by-zone")
def get_targets_by_zone(period: str = Query("2026-02")):
    """Target vs actual by zone/region."""
    conn = get_conn()
    try:
        year, month = period.split('-')
        
        rows = query_all(conn, """
            SELECT 
                t.zone,
                SUM(t.target_amount) as target,
                COALESCE(SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END), 0) as actual,
                COUNT(DISTINCT t.site_code) as stores
            FROM dim_store_targets t
            LEFT JOIN dim_stores s ON t.site_code = s.site_code
            LEFT JOIN fact_sales f ON s.site_code = f.site_code
                AND EXTRACT(YEAR FROM f.sale_date) = ?
                AND EXTRACT(MONTH FROM f.sale_date) = ?
            WHERE t.target_period = ?
            GROUP BY t.zone
            ORDER BY SUM(t.target_amount) DESC
        """, [int(year), int(month), period])
        
        zones = []
        for r in rows:
            zone, target, actual, stores = r
            achievement = round(100 * actual / target, 1) if target else 0
            zones.append({
                "zone": zone or "Unclassified",
                "target": float(target or 0),
                "actual": float(actual),
                "achievement": achievement,
                "stores": int(stores)
            })
        
        return {"zones": zones}
    finally:
        conn.close()


@router.get("/targets/by-store")
def get_targets_by_store(region: str = Query("All Regions"), period: str = Query("2026-02")):
    """Target vs actual by individual store."""
    conn = get_conn()
    try:
        year, month = period.split('-')
        
        if region == "All Regions":
            sql = """
                SELECT 
                    t.site_code,
                    s.site_name,
                    t.zone,
                    t.store_category,
                    t.grade,
                    t.target_amount,
                    COALESCE(SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END), 0) as actual
                FROM dim_store_targets t
                LEFT JOIN dim_stores s ON t.site_code = s.site_code
                LEFT JOIN fact_sales f ON s.site_code = f.site_code
                    AND EXTRACT(YEAR FROM f.sale_date) = ?
                    AND EXTRACT(MONTH FROM f.sale_date) = ?
                WHERE t.target_period = ?
                GROUP BY t.site_code, s.site_name, t.zone, t.store_category, t.grade, t.target_amount
                ORDER BY t.target_amount DESC
            """
            params = [int(year), int(month), period]
        else:
            sql = """
                SELECT 
                    t.site_code,
                    s.site_name,
                    t.zone,
                    t.store_category,
                    t.grade,
                    t.target_amount,
                    COALESCE(SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END), 0) as actual
                FROM dim_store_targets t
                LEFT JOIN dim_stores s ON t.site_code = s.site_code
                LEFT JOIN fact_sales f ON s.site_code = f.site_code
                    AND EXTRACT(YEAR FROM f.sale_date) = ?
                    AND EXTRACT(MONTH FROM f.sale_date) = ?
                WHERE t.target_period = ? AND t.zone = ?
                GROUP BY t.site_code, s.site_name, t.zone, t.store_category, t.grade, t.target_amount
                ORDER BY t.target_amount DESC
            """
            params = [int(year), int(month), period, region]
        
        rows = query_all(conn, sql, params)
        
        stores = []
        for r in rows:
            site_code, site_name, zone, category, grade, target, actual = r
            achievement = round(100 * actual / target, 1) if target else 0
            stores.append({
                "siteCode": site_code,
                "siteName": site_name or "Unknown",
                "zone": zone or "Unclassified",
                "category": category,
                "grade": grade,
                "target": float(target or 0),
                "actual": float(actual),
                "achievement": achievement,
                "gap": float(target - actual) if target else 0
            })
        
        return {"stores": stores}
    finally:
        conn.close()
