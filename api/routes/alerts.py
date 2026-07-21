"""Alerts API — derived from inventory and sales data (low stock, overstock, slow-moving)."""
from fastapi import APIRouter, Query
from api.db import get_conn, query_all
from api.config import DATABASE_BACKEND

router = APIRouter()

_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"


def _fetch_alerts_dax(days: int = 30, limit: int = 30, season: str = "All Seasons") -> dict:
    """Generate comprehensive alerts (Low Stock, Overstock, Slow-Moving) via DAX."""
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, C_SALE_DATE, C_SALE_SITE, C_SALE_AMOUNT, C_SALE_QTY, C_SALE_IS_RETURN, C_SALE_BARCODE, C_SALE_DISCOUNT, T_FI, C_INV_QTY, T_DP, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    d = max(int(days), 1)
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    
    season_filter = ""
    if season and season != "All Seasons":
         season_filter = f", KEEPFILTERS(FILTER(ALL('{T_DS}'[{C_STORE_ZONE}]), '{T_DS}'[{C_STORE_ZONE}] = \"{season}\"))"

    alerts = []

    try:
        # Optimized query to get top 5 lowest stock sites from Dim_SKU_Store_Season
        q = f"""
EVALUATE
TOPN(5,
    FILTER(
        SUMMARIZECOLUMNS(
            'Dim_SKU_Store_Season'[Site_Name]
            {season_filter},
            "Net_Stock", [Qty_Received] - [Qty_Sold]
        ),

        NOT(LEFT(UPPER('Dim_SKU_Store_Season'[Site_Name]), 13) = "FRESCO GLOBAL") &&
        [Net_Stock] > 0
    ),
    [Net_Stock],
    ASC
)
ORDER BY [Net_Stock] ASC
"""
        rows = execute_dax(q)
        
        for r in (rows or []):
            rn = _norm_row(r)
            site = rn.get("SITE_NAME") or rn.get("Site_Name")
            curr = float(rn.get("NET_STOCK") or rn.get("Net_Stock") or 0)
            
            alerts.append({
                "id": f"low-{site}",
                "type": "Low Stock",
                "title": "Low Stock Alert",
                "description": f"Site {site} — {int(curr)} units remaining",
                "timeAgo": "Recent",
                "severity": "high" if curr == 0 else "medium",
                "affectedStores": 1, "affectedSKUs": 1,
                "metrics": [{"label": "Current Stock", "value": f"{int(curr)}", "status": "critical"}],
            })

        # The other alerts (Overstock/Slow-moving) can keep a simplified version of the old query
        # without T_DS[Site Name] to avoid the DatasetExecuteQueriesError.
        # Temporarily disabled because the massive SKUxSite cross-join times out Power BI DAX.
        rows = []

        # 2. Overstock Alerts (Grouped by SKU)
        from collections import defaultdict
        sku_stock = defaultdict(lambda: {"qty": 0, "stores": 0, "sku": ""})
        for r in (rows or []):
            rn = _norm_row(r)
            bc = rn.get("Barcode")
            sku_stock[bc]["qty"] += int(rn.get("qty") or 0)
            sku_stock[bc]["stores"] += 1
            sku_stock[bc]["sku"] = rn.get("SKU") or bc
            sku_stock[bc]["sales_n"] = sku_stock[bc].get("sales_n", 0) + int(rn.get("sales_n") or 0)

        overstock_limit = 5
        overstock = [k for k, v in sku_stock.items() if v["qty"] > 1000]
        for bc in overstock[:overstock_limit]:
            v = sku_stock[bc]
            val_cr = round(v["qty"] * 250 / 1e7, 1) # Estimated value proxy
            alerts.append({
                "id": f"over-{bc}",
                "type": "Overstock",
                "title": "Overstock Risk",
                "description": f"{v['sku']} — {v['qty']:,} units across {v['stores']} stores",
                "timeAgo": "Recent",
                "severity": "high" if v["qty"] > 3000 else "medium",
                "affectedStores": v["stores"], "affectedSKUs": 1,
                "metrics": [
                    {"label": "Total Units", "value": f"{v['qty']:,}", "status": "critical"},
                    {"label": "Stores", "value": str(v["stores"]), "status": "normal"},
                ],
            })

        # 3. Slow-moving Alerts
        slow_moving_limit = 5
        slow_moving = [k for k, v in sku_stock.items() if v["qty"] > 20 and v.get("sales_n", 0) == 0]
        for bc in slow_moving[:slow_moving_limit]:
            v = sku_stock[bc]
            alerts.append({
                "id": f"idle-{bc}",
                "type": "Slow-Moving",
                "title": "Slow-Moving Alert",
                "description": f"{v['sku']} — {v['qty']:,} units idle in last {d} days",
                "timeAgo": f"No sales {d}d",
                "severity": "medium",
                "affectedStores": v["stores"], "affectedSKUs": 1,
                "metrics": [{"label": "Idle Units", "value": f"{v['qty']:,}", "status": "warning"}],
            })

    except Exception as e:
        import logging; logging.getLogger(__name__).warning("Alerts DAX failed: %s", e)
    
    return {"alerts": alerts[:limit]}


@router.get("/alerts")
def get_alerts(region: str = Query("All Regions"), season: str = Query("All Seasons"), days: int = Query(30), limit: int = Query(30)):
    """Alerts derived from inventory and sales data."""
    if _USE_POWERBI:
        return _fetch_alerts_dax(days, limit, season)

    try:
        conn = get_conn()
    except Exception:
        return {"alerts": []}
    alerts = []
    params = [region] if region != "All Regions" else []

    try:
        max_snap = "SELECT MAX(snapshot_date) FROM fact_inventory_snapshots"

        # Low stock: DOS < 14 based on 30-day velocity
        interval = 30
        if region == "All Regions":
            low_sql = f"""
                WITH sku_stats AS (
                    SELECT i.barcode, p.sku_code, s.site_name, s.zone,
                           SUM(i.quantity) as stock,
                           (SELECT SUM(f.quantity) FROM fact_sales f 
                            WHERE f.barcode = i.barcode AND f.site_code = i.site_code AND NOT f.is_return 
                            AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY) as sold
                    FROM fact_inventory_snapshots i
                    LEFT JOIN dim_products p ON i.barcode = p.barcode
                    LEFT JOIN dim_stores s ON i.site_code = s.site_code
                    WHERE i.snapshot_date = ({max_snap}) AND i.site_code IS NOT NULL
                    AND NOT (s.site_name LIKE 'FRESCO GLOBAL%')
                    GROUP BY 1, 2, 3, 4, i.site_code
                    HAVING SUM(i.quantity) > 0
                )
                SELECT barcode, sku_code, stock, site_name, zone, (stock * {interval}.0 / NULLIF(sold, 0)) as dos
                FROM sku_stats
                WHERE sold > 0 AND (stock * {interval}.0 / sold) < 14
                ORDER BY dos ASC
                LIMIT 5
            """
            low_params = []
        else:
            low_sql = f"""
                WITH sku_stats AS (
                    SELECT i.barcode, p.sku_code, s.site_name, s.zone,
                           SUM(i.quantity) as stock,
                           (SELECT SUM(f.quantity) FROM fact_sales f 
                            WHERE f.barcode = i.barcode AND f.site_code = i.site_code AND NOT f.is_return 
                            AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{interval}' DAY) as sold
                    FROM fact_inventory_snapshots i
                    LEFT JOIN dim_products p ON i.barcode = p.barcode
                    JOIN dim_stores s ON i.site_code = s.site_code
                    WHERE i.snapshot_date = ({max_snap}) AND s.zone = ?
                    AND NOT (s.site_name LIKE 'FRESCO GLOBAL%')
                    GROUP BY 1, 2, 3, 4, i.site_code
                    HAVING SUM(i.quantity) > 0
                )
                SELECT barcode, sku_code, stock, site_name, zone, (stock * {interval}.0 / NULLIF(sold, 0)) as dos
                FROM sku_stats
                WHERE sold > 0 AND (stock * {interval}.0 / sold) < 14
                ORDER BY dos ASC
                LIMIT 5
            """
            low_params = params
        low_rows = query_all(conn, low_sql, low_params)
        for r in low_rows:
            sku = r[1] or r[0]
            store = r[3] or r[4] or "Store"
            dos_val = r[5]
            alerts.append({
                "id": f"low-{r[0]}-{r[4] or r[0]}",
                "type": "Low Stock",
                "title": "Low Stock Alert",
                "description": f"{sku} at {store} — {int(dos_val)} days supply remaining",
                "timeAgo": "Recent",
                "severity": "high" if dos_val < 7 else "medium",
                "affectedStores": 1,
                "affectedSKUs": 1,
                "metrics": [
                    {"label": "DOS", "value": f"{int(dos_val)} days", "status": "critical"},
                    {"label": "Stock", "value": f"{int(r[2])} units", "status": "warning"},
                ],
            })

        # Overstock: high inventory by product
        if region == "All Regions":
            over_sql = f"""
                SELECT i.barcode, p.sku_code, SUM(i.quantity) qty,
                       COUNT(DISTINCT i.site_code) store_count,
                       SUM(i.quantity * COALESCE(p.mrp, 0)) as total_value
                FROM fact_inventory_snapshots i
                LEFT JOIN dim_products p ON i.barcode = p.barcode
                WHERE i.snapshot_date = ({max_snap})
                GROUP BY i.barcode, p.sku_code
                HAVING SUM(i.quantity) > 1000
            """
            over_params = []
        else:
            over_sql = f"""
                SELECT i.barcode, p.sku_code, SUM(i.quantity) qty,
                       COUNT(DISTINCT i.site_code) store_count,
                       SUM(i.quantity * COALESCE(p.mrp, 0)) as total_value
                FROM fact_inventory_snapshots i
                LEFT JOIN dim_products p ON i.barcode = p.barcode
                JOIN dim_stores s ON i.site_code = s.site_code
                WHERE i.snapshot_date = ({max_snap}) AND s.zone = ?
                GROUP BY i.barcode, p.sku_code
                HAVING SUM(i.quantity) > 1000
            """
            over_params = params
        over_rows = query_all(conn, over_sql, over_params)

        for r in over_rows[:6]:
            sku = r[1] or r[0]
            # r[4] is total_value (Decimal)
            val_cr = round(float(r[4] or 0) / 1e7, 2)
            alerts.append({
                "id": f"over-{r[0]}",
                "type": "Overstock",
                "title": "Overstock Risk",
                "description": f"{sku} — {r[2]} units across {r[3]} stores",
                "timeAgo": "Recent",
                "severity": "high" if r[2] > 3000 else "medium",
                "affectedStores": r[3],
                "affectedSKUs": 1,
                "impactValue": val_cr,
                "metrics": [
                    {"label": "Total Units", "value": f"{r[2]:,}", "status": "critical"},
                    {"label": "Est. Value", "value": f"₹{val_cr}Cr", "status": "warning"},
                    {"label": "Stores", "value": str(r[3]), "status": "normal"},
                ],
            })

        # Slow-moving: DOS > 90 based on 30-day velocity
        if region == "All Regions":
            idle_sql = f"""
                WITH sku_stats AS (
                    SELECT i.barcode, p.sku_code,
                           SUM(i.quantity) as stock,
                           (SELECT SUM(f.quantity) FROM fact_sales f 
                            WHERE f.barcode = i.barcode AND NOT f.is_return 
                            AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '30' DAY) as sold
                    FROM fact_inventory_snapshots i
                    LEFT JOIN dim_products p ON i.barcode = p.barcode
                    WHERE i.snapshot_date = ({max_snap})
                    GROUP BY 1, 2
                )
                SELECT barcode, sku_code, stock, (stock * 30.0 / NULLIF(sold, 0)) as dos
                FROM sku_stats
                WHERE dos > 90 OR (stock > 10 AND sold IS NULL)
                ORDER BY stock DESC
                LIMIT 5
            """
            idle_params = []
        else:
            idle_sql = f"""
                WITH sku_stats AS (
                    SELECT i.barcode, p.sku_code,
                           SUM(i.quantity) as stock,
                           (SELECT SUM(f.quantity) FROM fact_sales f 
                            JOIN dim_stores s2 ON f.site_code = s2.site_code
                            WHERE f.barcode = i.barcode AND NOT f.is_return AND s2.zone = ?
                            AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '30' DAY) as sold
                    FROM fact_inventory_snapshots i
                    LEFT JOIN dim_products p ON i.barcode = p.barcode
                    JOIN dim_stores s ON i.site_code = s.site_code
                    WHERE i.snapshot_date = ({max_snap}) AND s.zone = ?
                    GROUP BY 1, 2
                )
                SELECT barcode, sku_code, stock, (stock * 30.0 / NULLIF(sold, 0)) as dos
                FROM sku_stats
                WHERE dos > 90 OR (stock > 10 AND sold IS NULL)
                ORDER BY stock DESC
                LIMIT 5
            """
            idle_params = [region, region]

        try:
            idle_rows = query_all(conn, idle_sql, idle_params)
        except Exception as e:
            print(f"Error fetching idle alerts: {e}")
            idle_rows = []

        for r in idle_rows:
            sku = r[1] or r[0]
            dos_val = r[3] or 999
            alerts.append({
                "id": f"idle-{r[0]}",
                "type": "Slow-Moving",
                "title": "Slow-Moving Inventory",
                "description": f"{sku} — high days of supply ({int(dos_val)} days)",
                "timeAgo": "Recent",
                "severity": "high" if dos_val > 180 else "medium",
                "affectedStores": "Global",
                "affectedSKUs": 1,
                "metrics": [
                    {"label": "DOS", "value": f"{int(dos_val)} days", "status": "critical"},
                    {"label": "Stock", "value": f"{int(r[2])} units", "status": "warning"},
                ],
            })

        return {"alerts": alerts[:limit]}
    except Exception as e:
        print(f"Error in get_alerts: {e}")
        return {"alerts": [], "error": str(e)}
    finally:
        conn.close()

