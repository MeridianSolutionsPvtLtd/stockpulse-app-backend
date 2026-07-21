"""Stores API — store list with metrics from DuckDB or Power BI Semantic Model."""
from fastapi import APIRouter, Query
from api.db import get_conn, query_all
from api.config import DATABASE_BACKEND

router = APIRouter()

_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"


def _norm(row: dict) -> dict:
    """Strip [brackets] from DAX ROW() result keys."""
    return {k.strip("[]"): v for k, v in row.items()}


def _fetch_stores_dax(days: int = 30, limit: int = 100, offset: int = 0, sort_by: str = "revenue", sort_order: str = "desc", 
                      season: str = "All Seasons", sku: str = "All SKUs", site: str = "All Stores", month_year: str = "All Months") -> dict:
    """Use DAX to get store-level performance from Fact_Sales_Detail using native measures."""
    from api.db_powerbi import execute_dax
    from api.dax_queries import _norm_row
    from api.schema_config import T_DS, C_STORE_ZONE
    
    try:
        dax_filters = []
        if season and season != "All Seasons":
            dax_filters.append(f"KEEPFILTERS(FILTER(ALL('{T_DS}'[{C_STORE_ZONE}]), '{T_DS}'[{C_STORE_ZONE}] = \"{season}\"))")
        if sku and sku != "All SKUs":
            dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Dim_SKU_Store_Season'[SKU]), 'Dim_SKU_Store_Season'[SKU] = \"{sku}\"))")
        if site and site != "All Stores":
            dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Dim_SKU_Store_Season'[Site_Name]), 'Dim_SKU_Store_Season'[Site_Name] = \"{site}\"))")
        if month_year and month_year != "All Months":
            dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Date_Table'[Month-Year]), 'Date_Table'[Month-Year] = \"{month_year}\"))")
        
        filter_str = ",\n    ".join(dax_filters) if dax_filters else ""

        # Whole dataset query as requested
        q = f"""
EVALUATE
SUMMARIZECOLUMNS(
    'Dim_SKU_Store_Season'[Site_Name],
    {filter_str + "," if filter_str else ""}
    "revenue", [Sales_Amt],
    "units_sold", [Qty_Sold],
    "sell_through", [Sell Through %],
    "disc_val", [1Discount %]
)
"""

        rows = execute_dax(q)
        stores = []
        for r in (rows or []):
            rn = _norm_row(r)
            site = str(rn.get("SITE_NAME") or rn.get("Site_Name") or rn.get("SITE") or "Unknown")
            
            rev = float(rn.get("revenue") or rn.get("REVENUE") or 0)
            sold_count = int(float(rn.get("units_sold") or rn.get("UNITS_SOLD") or 0))
            
            # Use Power BI native Sell_Through measure
            st_raw = float(rn.get("sell_through") or rn.get("SELL_THROUGH") or 0)
            st = round(st_raw * 100, 1) if st_raw <= 1 else round(st_raw, 1)
                
            if st > 100: st = 100.0
            
            # Discount rate mapping
            disc_raw = float(rn.get("DISC_VAL") or rn.get("disc_val") or 0)
            discount = round(disc_raw * 100, 1) if disc_raw < 1 else round(disc_raw, 1)
            
            if not site or site == "Unknown":
                continue
                
            status = "Optimal" if st >= 70 else "Needs Attention" if st >= 50 else "Critical"
            
            stores.append({
                "id": site, 
                "name": site, 
                "region": "All Regions",
                "sellThrough": st, 
                "inventoryValue": rev,
                "discountRate": discount, 
                "status": status,
                "trend": 0, 
                "revenue": rev,
                "unitsSold": sold_count 
            })
            
        # Global sorting
        reverse = sort_order.lower() == "desc"
        def _sort_key(x):
            val = x.get(sort_by)
            if val is None:
                return 0
            if isinstance(val, str):
                return val.lower()
            return val
            
        stores.sort(key=_sort_key, reverse=reverse)
        
        total = len(stores)
        paginated_stores = stores[offset:offset + limit]

        return {
            "stores": paginated_stores,
            "total": total,
            "limit": limit,
            "offset": offset,
            "sortBy": sort_by,
            "sortOrder": sort_order
        }
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("Stores DAX failed: %s", e)
        return {"stores": [], "total": 0, "limit": limit, "offset": offset, "sortBy": sort_by, "sortOrder": sort_order}


def _fetch_regions_dax() -> dict:
    """Use DAX to get distinct zone/season values from the stores/sales table."""
    from api.db_powerbi import execute_dax
    try:
        rows = execute_dax(f"EVALUATE DISTINCT('Fact_Sales_Detail'[Site])")
        sites = sorted({list(r.values())[0] for r in rows if r and list(r.values())[0]})
        return {"regions": ["All Regions"], "zoneList": ["All Regions"]}
    except Exception:
        return {"regions": ["All Regions"], "zoneList": ["All Regions"]}


def _date_filter(days: int | None, alias: str = ""):
    if not days or days <= 0:
        return "", []
    d = int(days)
    prefix = f"{alias}." if alias else ""
    return f" AND {prefix}sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY", []


def _purchase_date_filter(days: int | None):
    if not days or days <= 0:
        return "", []
    d = int(days)
    return f" AND purchase_date >= (SELECT MAX(purchase_date) FROM fact_purchases) - INTERVAL '{d}' DAY", []


@router.get("/stores")
def get_stores(
    region: str = Query("All Regions"),
    season: str = Query("All Seasons"),
    sku: str = Query("All SKUs"),
    site: str = Query("All Stores"),
    month_year: str = Query("All Months"),
    search: str = Query(""),
    days: int = Query(30, description="Last N days (7, 14, 30, 60)"),
    limit: int = Query(100, ge=1),
    offset: int = Query(0, ge=0),
    sort_by: str = Query("revenue", description="Field to sort by"),
    sort_order: str = Query("desc", description="asc or desc")
):
    """Stores with sell-through, metrics. Supports filtering, sorting, pagination."""
    if _USE_POWERBI:
        return _fetch_stores_dax(days, limit, offset, sort_by, sort_order, season, sku, site, month_year)

    conn = get_conn()
    try:
        df, df_params = _date_filter(days, "f")
        # ... (keep existing query logic) ...
        df_sales, df_sales_params = _date_filter(days, "")  # for queries without alias
        if region == "All Regions":
            sql = f"""
                SELECT s.site_code, s.site_name, s.zone, s.city,
                       COALESCE(SUM(CASE WHEN f.is_return = false THEN f.net_amount ELSE 0 END), 0) revenue,
                       COALESCE(SUM(CASE WHEN f.is_return = false THEN f.quantity ELSE 0 END), 0) units_sold
                FROM dim_stores s
                INNER JOIN fact_sales f ON s.site_code = f.site_code AND 1=1 {df}
                INNER JOIN dim_products p ON f.barcode = p.barcode
                GROUP BY s.site_code, s.site_name, s.zone, s.city
                HAVING COALESCE(SUM(CASE WHEN f.is_return = false THEN f.quantity ELSE 0 END), 0) > 0
            """
            params = df_params
        else:
            sql = f"""
                SELECT s.site_code, s.site_name, s.zone, s.city,
                       COALESCE(SUM(CASE WHEN f.is_return = false THEN f.net_amount ELSE 0 END), 0) revenue,
                       COALESCE(SUM(CASE WHEN f.is_return = false THEN f.quantity ELSE 0 END), 0) units_sold
                FROM dim_stores s
                INNER JOIN fact_sales f ON s.site_code = f.site_code {df}
                INNER JOIN dim_products p ON f.barcode = p.barcode
                WHERE s.zone = ?
                GROUP BY s.site_code, s.site_name, s.zone, s.city
                HAVING COALESCE(SUM(CASE WHEN f.is_return = false THEN f.quantity ELSE 0 END), 0) > 0
            """
            params = [region] + df_params

        rows = query_all(conn, sql, params)

        inv_sql = """
            WITH source_totals AS (
                SELECT site_code, barcode, source, SUM(quantity) as quantity
                FROM fact_inventory_snapshots
                WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND site_code IS NOT NULL
                GROUP BY site_code, barcode, source
            ),
            latest_snap AS (
                SELECT site_code, barcode, MAX(quantity) as quantity
                FROM source_totals
                GROUP BY site_code, barcode
            )
            SELECT i.site_code, 
                   SUM(i.quantity) as qty,
                   SUM(i.quantity * COALESCE(p.mrp, 0)) as val
            FROM latest_snap i
            LEFT JOIN dim_products p ON i.barcode = p.barcode
            GROUP BY i.site_code
        """
        inv_rows = query_all(conn, inv_sql)
        inv_map = {str(r[0]): (int(r[1]), float(r[2] or 0)) for r in inv_rows}

        # Idle inventory
        df_idle, df_idle_params = _date_filter(days, "f") if days and days > 0 else ("", [])
        idle_sql = f"""
            WITH store_sold AS (
                SELECT f.site_code, f.barcode FROM fact_sales f
                WHERE NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                  {df_idle}
                GROUP BY f.site_code, f.barcode
            ),
            source_totals AS (
                SELECT site_code, barcode, source, SUM(quantity) as quantity
                FROM fact_inventory_snapshots
                WHERE snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND site_code IS NOT NULL AND barcode IS NOT NULL AND TRIM(barcode) != ''
                GROUP BY site_code, barcode, source
            ),
            store_inv AS (
                SELECT site_code, barcode, MAX(quantity) as quantity 
                FROM source_totals
                GROUP BY site_code, barcode
            )
            SELECT i.site_code, 
                   COALESCE(SUM(i.quantity), 0),
                   COALESCE(SUM(i.quantity * COALESCE(p.mrp, 0)), 0)
            FROM store_inv i
            LEFT JOIN store_sold s ON i.site_code = s.site_code AND i.barcode = s.barcode
            LEFT JOIN dim_products p ON i.barcode = p.barcode
            WHERE s.barcode IS NULL
            GROUP BY i.site_code
        """
        idle_rows = query_all(conn, idle_sql, df_idle_params)
        idle_map = {str(r[0]): (int(r[1]), float(r[2] or 0)) for r in idle_rows}

        # Get purchases
        dp, dp_params = _purchase_date_filter(days)
        purch_sql = f"""
            SELECT site_code, SUM(quantity) qty FROM fact_purchases
            WHERE site_code IS NOT NULL {dp}
            GROUP BY site_code
        """
        purch_rows = query_all(conn, purch_sql, dp_params)
        purch_map = {str(r[0]): int(r[1]) for r in purch_rows}

        # Avg discount
        disc_sql = f"""
            SELECT site_code, AVG(discount_pct) FROM fact_sales
            WHERE site_code IS NOT NULL AND NOT is_return AND discount_pct BETWEEN 0 AND 80 {df_sales}
            GROUP BY site_code
        """
        disc_rows = query_all(conn, disc_sql, df_sales_params)
        disc_map = {str(r[0]): round(float(r[1] or 0), 1) for r in disc_rows}

        # Previous period revenue for trend calculation
        d = int(days) if days else 30
        prev_sql = f"""
            SELECT f.site_code, SUM(f.net_amount) 
            FROM fact_sales f
            JOIN dim_products p ON f.barcode = p.barcode
            WHERE f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d * 2}' DAY
              AND f.sale_date < (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY
              AND NOT f.is_return
            GROUP BY f.site_code
        """
        prev_rows = query_all(conn, prev_sql)
        prev_map = {str(r[0]): float(r[1] or 0) for r in prev_rows}

        # Build store list
        stores = []
        for r in rows:
            site_code, site_name, zone, city, revenue, units_sold = r
            inv_qty, inv_val = inv_map.get(str(site_code), (0, 0.0))
            purch = purch_map.get(str(site_code), 0)
            
            # Sell-through: sold / purchased when available; else sold / (sold + stock)
            if purch > 0:
                st = min(round(100 * units_sold / purch, 1), 100)
            elif units_sold + inv_qty > 0:
                st = round(100 * units_sold / (units_sold + inv_qty), 1)
            else:
                st = 0
            
            discount = disc_map.get(str(site_code), 0)

            # Unique name logic
            raw_name = (site_name or site_code or "").strip()
            city_str = (city or "").strip()
            display_name = f"{raw_name} ({city_str})" if city_str and city_str.lower() not in raw_name.lower() else raw_name

            if search:
                q = search.lower().strip()
                if q not in display_name.lower() and q not in (site_code or "").lower():
                    continue

            status = "Optimal" if st >= 70 and discount < 20 else ("Needs Attention" if st >= 50 else "Critical")
            idle_units, idle_val = idle_map.get(str(site_code), (0, 0.0))
            
            # Skip stores with zero revenue after filtering (unmapped barcodes only)
            curr_rev = float(revenue or 0)
            if curr_rev == 0:
                continue

            # Trend calculation
            prev_rev = prev_map.get(str(site_code), 0)
            if prev_rev > 0:
                trend_val = round(((curr_rev - prev_rev) / prev_rev) * 100, 1)
            elif curr_rev > 0:
                trend_val = 100.0  # New sales from zero
            else:
                trend_val = 0.0

            stores.append({
                "id": site_code,
                "name": display_name,
                "region": zone or "Unclassified",
                "sellThrough": st,
                "inventoryValue": inv_val,
                "idleInventory": idle_units,
                "idleInventoryValue": idle_val,
                "discountRate": discount,
                "status": status,
                "trend": trend_val,
                "revenue": curr_rev,
                "unitsSold": int(units_sold),
            })
        
        # Sorting
        reverse = sort_order.lower() == "desc"
        stores.sort(key=lambda x: x.get(sort_by, 0) if isinstance(x.get(sort_by), (int, float)) else str(x.get(sort_by, "")).lower(), reverse=reverse)

        # Pagination
        total_count = len(stores)
        paginated_stores = stores[offset : offset + limit] if limit > 0 else stores

        return {
            "stores": paginated_stores,
            "total": total_count,
            "limit": limit,
            "offset": offset,
            "sortBy": sort_by,
            "sortOrder": sort_order
        }
    finally:
        conn.close()


@router.get("/stores/idle-summary")
def get_idle_summary(region: str = Query("All Regions"), days: int = Query(30, description="Last N days")):
    """Idle inventory summary with age buckets and category breakdown."""
    conn = get_conn()
    try:
        d = int(days) if days and days > 0 else 30
        region_join = ""
        region_where = ""
        region_params: list = []
        if region != "All Regions":
            region_join = "JOIN dim_stores st ON i.site_code = st.site_code"
            region_where = "AND st.zone = ?"
            region_params = [region]

        sql = f"""
            WITH max_date AS (
                SELECT MAX(sale_date) AS ref_date FROM fact_sales
            ),
            store_inv AS (
                SELECT i.site_code, i.barcode, i.quantity
                FROM fact_inventory_snapshots i
                {region_join}
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.site_code IS NOT NULL AND i.barcode IS NOT NULL
                  AND TRIM(i.barcode) != '' AND i.quantity > 0
                  {region_where}
            ),
            recent_sold AS (
                SELECT f.site_code, f.barcode
                FROM fact_sales f
                WHERE NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                  AND f.sale_date >= (SELECT ref_date FROM max_date) - INTERVAL '{d}' DAY
                GROUP BY f.site_code, f.barcode
            ),
            all_last_sale AS (
                SELECT f.site_code, f.barcode, MAX(f.sale_date) AS last_sale
                FROM fact_sales f
                WHERE NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                GROUP BY f.site_code, f.barcode
            ),
            idle_items AS (
                SELECT inv.site_code, inv.barcode, inv.quantity,
                       als.last_sale,
                       COALESCE(
                           DATEDIFF('day', als.last_sale, (SELECT ref_date FROM max_date)),
                           90
                       ) AS age_days
                FROM store_inv inv
                LEFT JOIN recent_sold rs ON inv.site_code = rs.site_code AND inv.barcode = rs.barcode
                LEFT JOIN all_last_sale als ON inv.site_code = als.site_code AND inv.barcode = als.barcode
                WHERE rs.barcode IS NULL
            ),
            idle_with_cat AS (
                SELECT ii.site_code, ii.barcode, ii.quantity, ii.age_days,
                       COALESCE(p.department, p.section, 'Unclassified') AS category,
                       COALESCE(p.mrp, 0) AS unit_price
                FROM idle_items ii
                LEFT JOIN dim_products p ON ii.barcode = p.barcode
            )
            SELECT
                category,
                SUM(quantity) AS idle_units,
                SUM(quantity * unit_price) AS idle_value,
                ROUND(AVG(age_days), 0) AS avg_age,
                COUNT(DISTINCT site_code) AS stores_affected,
                SUM(CASE WHEN age_days <= 30 THEN quantity ELSE 0 END) AS bucket_0_30,
                SUM(CASE WHEN age_days > 30 AND age_days <= 60 THEN quantity ELSE 0 END) AS bucket_30_60,
                SUM(CASE WHEN age_days > 60 AND age_days <= 90 THEN quantity ELSE 0 END) AS bucket_60_90,
                SUM(CASE WHEN age_days > 90 THEN quantity ELSE 0 END) AS bucket_90_plus
            FROM idle_with_cat
            GROUP BY category
            ORDER BY idle_units DESC
        """
        rows = query_all(conn, sql, region_params)

        total_units = 0
        total_value = 0
        total_age_weighted = 0
        stores_set: set = set()
        buckets = [0, 0, 0, 0]
        categories = []

        for r in rows:
            cat, units, value, avg_age, stores, b0, b1, b2, b3 = r
            units = int(units or 0)
            value = float(value or 0)
            avg_age = int(avg_age or 0)
            stores = int(stores or 0)
            if units == 0:
                continue
            total_units += units
            total_value += value
            total_age_weighted += avg_age * units
            buckets[0] += int(b0 or 0)
            buckets[1] += int(b1 or 0)
            buckets[2] += int(b2 or 0)
            buckets[3] += int(b3 or 0)
            categories.append({
                "category": cat or "Unclassified",
                "idleUnits": units,
                "idleValue": value,
                "avgAgeDays": avg_age,
                "storesAffected": stores,
            })

        # Count distinct stores across all categories
        if region != "All Regions":
            stores_sql = f"""
                SELECT COUNT(DISTINCT inv.site_code)
                FROM fact_inventory_snapshots inv
                JOIN dim_stores st ON inv.site_code = st.site_code
                LEFT JOIN (
                    SELECT f.site_code, f.barcode FROM fact_sales f
                    WHERE NOT f.is_return AND f.quantity > 0
                      AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                      AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY
                    GROUP BY f.site_code, f.barcode
                ) sold ON inv.site_code = sold.site_code AND inv.barcode = sold.barcode
                WHERE inv.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND inv.quantity > 0 AND sold.barcode IS NULL
                  AND st.zone = ?
            """
            stores_total = query_all(conn, stores_sql, [region])[0][0] or 0
        else:
            stores_sql = f"""
                SELECT COUNT(DISTINCT inv.site_code)
                FROM fact_inventory_snapshots inv
                LEFT JOIN (
                    SELECT f.site_code, f.barcode FROM fact_sales f
                    WHERE NOT f.is_return AND f.quantity > 0
                      AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                      AND f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY
                    GROUP BY f.site_code, f.barcode
                ) sold ON inv.site_code = sold.site_code AND inv.barcode = sold.barcode
                WHERE inv.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND inv.quantity > 0 AND sold.barcode IS NULL
            """
            stores_total = query_all(conn, stores_sql)[0][0] or 0

        overall_avg_age = round(total_age_weighted / max(total_units, 1))
        bucket_labels = ["0-30d", "30-60d", "60-90d", "90+d"]
        age_buckets = []
        for i, label in enumerate(bucket_labels):
            pct = round(100 * buckets[i] / max(total_units, 1), 1)
            age_buckets.append({"label": label, "units": buckets[i], "pct": pct})

        # Region-wise idle breakdown
        region_filter_sql = "AND st.zone = ?" if region != "All Regions" else ""
        region_filter_params = [region] if region != "All Regions" else []
        region_sql = f"""
            WITH max_date AS (
                SELECT MAX(sale_date) AS ref_date FROM fact_sales
            ),
            store_inv AS (
                SELECT i.site_code, i.barcode, i.quantity
                FROM fact_inventory_snapshots i
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.site_code IS NOT NULL AND i.barcode IS NOT NULL
                  AND TRIM(i.barcode) != '' AND i.quantity > 0
            ),
            recent_sold AS (
                SELECT f.site_code, f.barcode
                FROM fact_sales f
                WHERE NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                  AND f.sale_date >= (SELECT ref_date FROM max_date) - INTERVAL '{d}' DAY
                GROUP BY f.site_code, f.barcode
            ),
            all_last_sale AS (
                SELECT f.site_code, f.barcode, MAX(f.sale_date) AS last_sale
                FROM fact_sales f
                WHERE NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                GROUP BY f.site_code, f.barcode
            ),
            idle_items AS (
                SELECT inv.site_code, inv.barcode, inv.quantity,
                       COALESCE(
                           DATEDIFF('day', als.last_sale, (SELECT ref_date FROM max_date)),
                           90
                       ) AS age_days,
                       COALESCE(p.mrp, 0) AS unit_price
                FROM store_inv inv
                LEFT JOIN recent_sold rs ON inv.site_code = rs.site_code AND inv.barcode = rs.barcode
                LEFT JOIN all_last_sale als ON inv.site_code = als.site_code AND inv.barcode = als.barcode
                LEFT JOIN dim_products p ON inv.barcode = p.barcode
                WHERE rs.barcode IS NULL
            )
            SELECT st.zone,
                   SUM(ii.quantity) AS idle_units,
                   SUM(ii.quantity * ii.unit_price) AS idle_value,
                   ROUND(AVG(ii.age_days), 0) AS avg_age,
                   COUNT(DISTINCT ii.site_code) AS idle_store_count,
                   (SELECT COUNT(DISTINCT s2.site_code) FROM dim_stores s2 WHERE s2.zone = st.zone) AS total_stores
            FROM idle_items ii
            JOIN dim_stores st ON ii.site_code = st.site_code
            WHERE st.zone IS NOT NULL AND TRIM(st.zone) != ''
              {region_filter_sql}
            GROUP BY st.zone
            ORDER BY idle_units DESC
        """
        region_rows = query_all(conn, region_sql, region_filter_params)
        regions_data = []
        for rr in region_rows:
            zone, r_units, r_value, r_age, r_idle_stores, r_total_stores = rr
            r_units = int(r_units or 0)
            if r_units == 0:
                continue
            regions_data.append({
                "region": zone,
                "idleUnits": r_units,
                "idleValue": float(r_value or 0),
                "avgAgeDays": int(r_age or 0),
                "storeCount": int(r_idle_stores or 0),
                "totalStores": int(r_total_stores or 0),
            })

        # Per-store idle breakdown
        store_idle_sql = f"""
            WITH max_date AS (
                SELECT MAX(sale_date) AS ref_date FROM fact_sales
            ),
            store_inv AS (
                SELECT i.site_code, i.barcode, i.quantity
                FROM fact_inventory_snapshots i
                {region_join}
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.site_code IS NOT NULL AND i.barcode IS NOT NULL
                  AND TRIM(i.barcode) != '' AND i.quantity > 0
                  {region_where}
            ),
            recent_sold AS (
                SELECT f.site_code, f.barcode
                FROM fact_sales f
                WHERE NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                  AND f.sale_date >= (SELECT ref_date FROM max_date) - INTERVAL '{d}' DAY
                GROUP BY f.site_code, f.barcode
            ),
            all_last_sale AS (
                SELECT f.site_code, f.barcode, MAX(f.sale_date) AS last_sale
                FROM fact_sales f
                WHERE NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                GROUP BY f.site_code, f.barcode
            ),
            idle_items AS (
                SELECT inv.site_code, inv.barcode, inv.quantity,
                       COALESCE(
                           DATEDIFF('day', als.last_sale, (SELECT ref_date FROM max_date)),
                           90
                       ) AS age_days,
                       COALESCE(p.mrp, 0) AS unit_price,
                       COALESCE(p.department, p.section, 'Unclassified') AS category
                FROM store_inv inv
                LEFT JOIN recent_sold rs ON inv.site_code = rs.site_code AND inv.barcode = rs.barcode
                LEFT JOIN all_last_sale als ON inv.site_code = als.site_code AND inv.barcode = als.barcode
                LEFT JOIN dim_products p ON inv.barcode = p.barcode
                WHERE rs.barcode IS NULL
            ),
            store_agg AS (
                SELECT ii.site_code,
                       SUM(ii.quantity) AS idle_units,
                       SUM(ii.quantity * ii.unit_price) AS idle_value,
                       ROUND(AVG(ii.age_days), 0) AS avg_age
                FROM idle_items ii
                GROUP BY ii.site_code
            ),
            top_cat AS (
                SELECT ii.site_code, ii.category,
                       SUM(ii.quantity) AS cat_units,
                       ROW_NUMBER() OVER (PARTITION BY ii.site_code ORDER BY SUM(ii.quantity) DESC) AS rn
                FROM idle_items ii
                GROUP BY ii.site_code, ii.category
            )
            SELECT sa.site_code,
                   s.site_name,
                   s.zone,
                   sa.idle_units,
                   sa.idle_value,
                   sa.avg_age,
                   tc.category AS top_category,
                   tc.cat_units AS top_cat_units
            FROM store_agg sa
            JOIN dim_stores s ON sa.site_code = s.site_code
            LEFT JOIN top_cat tc ON sa.site_code = tc.site_code AND tc.rn = 1
            WHERE sa.idle_units > 0
            ORDER BY sa.idle_units DESC
        """
        store_idle_rows = query_all(conn, store_idle_sql, region_params)
        stores_data = []
        for sr in store_idle_rows:
            sc, sname, zone, s_units, s_value, s_age, top_cat_name, top_cat_units = sr
            stores_data.append({
                "siteCode": str(sc),
                "storeName": (sname or str(sc)).strip(),
                "region": zone or "Unclassified",
                "idleUnits": int(s_units or 0),
                "idleValue": float(s_value or 0),
                "avgAgeDays": int(s_age or 0),
                "topCategory": top_cat_name or "N/A",
                "topCategoryUnits": int(top_cat_units or 0),
            })

        return {
            "totalIdleUnits": total_units,
            "totalIdleValue": total_value,
            "avgAgeDays": overall_avg_age,
            "storesAffected": int(stores_total),
            "ageBuckets": age_buckets,
            "categories": categories,
            "regions": regions_data,
            "stores": stores_data,
        }
    finally:
        conn.close()


@router.get("/stores/{site_code}/idle-details")
def get_store_idle_details(site_code: str, days: int = Query(30, description="Last N days")):
    """Per-store idle inventory grouped by category, then by SKU/size."""
    conn = get_conn()
    try:
        d = int(days) if days and days > 0 else 30
        sql = f"""
            WITH max_date AS (
                SELECT MAX(sale_date) AS ref_date FROM fact_sales
            ),
            store_inv AS (
                SELECT i.barcode, i.quantity
                FROM fact_inventory_snapshots i
                WHERE i.site_code = ?
                  AND i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.barcode IS NOT NULL AND TRIM(i.barcode) != '' AND i.quantity > 0
            ),
            recent_sold AS (
                SELECT f.barcode
                FROM fact_sales f
                WHERE f.site_code = ? AND NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                  AND f.sale_date >= (SELECT ref_date FROM max_date) - INTERVAL '{d}' DAY
                GROUP BY f.barcode
            ),
            all_last_sale AS (
                SELECT f.barcode, MAX(f.sale_date) AS last_sale
                FROM fact_sales f
                WHERE f.site_code = ? AND NOT f.is_return AND f.quantity > 0
                  AND f.barcode IS NOT NULL AND TRIM(f.barcode) != ''
                GROUP BY f.barcode
            ),
            idle AS (
                SELECT inv.barcode, inv.quantity,
                       COALESCE(DATEDIFF('day', als.last_sale, (SELECT ref_date FROM max_date)), 90) AS age_days,
                       strftime(als.last_sale, '%Y-%m-%d') AS last_sale_date
                FROM store_inv inv
                LEFT JOIN recent_sold rs ON inv.barcode = rs.barcode
                LEFT JOIN all_last_sale als ON inv.barcode = als.barcode
                WHERE rs.barcode IS NULL
            )
            SELECT COALESCE(p.department, p.section, 'Unclassified') AS category,
                   p.sku_code,
                   idle.barcode,
                   COALESCE(p.size, 'N/A') AS size,
                   idle.quantity,
                   COALESCE(p.mrp, 0) AS mrp,
                   idle.quantity * COALESCE(p.mrp, 0) AS value,
                   idle.age_days,
                   idle.last_sale_date
            FROM idle
            LEFT JOIN dim_products p ON idle.barcode = p.barcode
            ORDER BY category, idle.quantity DESC
        """
        rows = query_all(conn, sql, [site_code, site_code, site_code])

        # Group by category, then list SKUs
        cat_map: dict = {}
        for r in rows:
            cat, sku, barcode, size, qty, mrp, value, age, last_sale = r
            cat = cat or 'Unclassified'
            if cat not in cat_map:
                cat_map[cat] = {"category": cat, "idleUnits": 0, "idleValue": 0, "avgAgeDays": 0, "_age_sum": 0, "_count": 0, "skus": []}
            entry = cat_map[cat]
            qty = int(qty or 0)
            value = float(value or 0)
            age = int(age or 0)
            entry["idleUnits"] += qty
            entry["idleValue"] += value
            entry["_age_sum"] += age * qty
            entry["_count"] += qty
            entry["skus"].append({
                "sku": sku or barcode,
                "barcode": str(barcode),
                "size": size or "N/A",
                "quantity": qty,
                "mrp": float(mrp or 0),
                "value": value,
                "ageDays": age,
                "lastSaleDate": last_sale,
            })

        categories = []
        for c in sorted(cat_map.values(), key=lambda x: -x["idleUnits"]):
            c["avgAgeDays"] = round(c["_age_sum"] / max(c["_count"], 1))
            del c["_age_sum"]
            del c["_count"]
            # Sort SKUs within category by quantity desc
            c["skus"] = sorted(c["skus"], key=lambda s: -s["quantity"])
            categories.append(c)

        total_units = sum(c["idleUnits"] for c in categories)
        total_value = sum(c["idleValue"] for c in categories)

        return {
            "siteCode": site_code,
            "totalIdleUnits": total_units,
            "totalIdleValue": total_value,
            "categories": categories,
        }
    finally:
        conn.close()


@router.get("/stores/{site_code}/details")
def get_store_details(site_code: str, days: int = Query(30, description="Last N days")):
    """Per-store category breakdown with real inventory, sales and sell-through data."""
    conn = get_conn()
    try:
        df, df_params = _date_filter(days, "f")
        dp, dp_params = _purchase_date_filter(days)

        # Category breakdown: sold, purchased, inventory, discount per department at this store
        cat_sql = f"""
            WITH cat_sales AS (
                SELECT COALESCE(p.department, p.section, 'Unclassified') AS category,
                       COALESCE(SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END), 0) AS sold,
                       COALESCE(SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END), 0) AS revenue,
                       AVG(CASE WHEN NOT f.is_return AND f.discount_pct BETWEEN 0 AND 80 THEN f.discount_pct END) AS avg_disc
                FROM fact_sales f
                LEFT JOIN dim_products p ON f.barcode = p.barcode
                WHERE f.site_code = ? {df}
                GROUP BY 1
            ),
            cat_purchases AS (
                SELECT COALESCE(p.department, p.section, 'Unclassified') AS category,
                       COALESCE(SUM(fp.quantity), 0) AS purchased
                FROM fact_purchases fp
                LEFT JOIN dim_products p ON fp.barcode = p.barcode
                WHERE fp.site_code = ? {dp}
                GROUP BY 1
            ),
            cat_inventory AS (
                SELECT COALESCE(p.department, p.section, 'Unclassified') AS category,
                       COALESCE(SUM(i.quantity), 0) AS stock
                FROM fact_inventory_snapshots i
                LEFT JOIN dim_products p ON i.barcode = p.barcode
                WHERE i.site_code = ?
                  AND i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                GROUP BY 1
            )
            SELECT COALESCE(s.category, p.category, inv.category) AS category,
                   COALESCE(s.sold, 0) AS sold,
                   COALESCE(p.purchased, 0) AS purchased,
                   COALESCE(inv.stock, 0) AS stock,
                   COALESCE(s.revenue, 0) AS revenue,
                   s.avg_disc
            FROM cat_sales s
            FULL OUTER JOIN cat_purchases p ON s.category = p.category
            FULL OUTER JOIN cat_inventory inv ON COALESCE(s.category, p.category) = inv.category
            ORDER BY COALESCE(s.revenue, 0) DESC
        """
        cat_params = [site_code] + df_params + [site_code] + dp_params + [site_code]
        cat_rows = query_all(conn, cat_sql, cat_params)

        categories = []
        for r in cat_rows:
            cat, sold, purchased, stock, revenue, avg_disc = r
            if not cat or (sold == 0 and purchased == 0 and stock == 0):
                continue
            st = min(round(100 * sold / purchased, 1), 100) if purchased else 0
            categories.append({
                "category": cat,
                "sold": int(sold),
                "purchased": int(purchased),
                "inventory": max(int(stock), 0),
                "sellThrough": st,
                "revenue": float(revenue),
                "discountRate": round(float(avg_disc or 0), 1),
            })

        # Top selling barcodes at this store
        top_sql = f"""
            SELECT f.barcode, p.sku_code, COALESCE(p.department, 'Unclassified'),
                   SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END) sold,
                   SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END) revenue
            FROM fact_sales f
            LEFT JOIN dim_products p ON f.barcode = p.barcode
            WHERE f.site_code = ? AND f.barcode IS NOT NULL {df}
            GROUP BY f.barcode, p.sku_code, 3
            ORDER BY sold DESC
            LIMIT 5
        """
        top_rows = query_all(conn, top_sql, [site_code] + df_params)
        top_products = [{"barcode": r[0], "sku": r[1], "category": r[2], "sold": int(r[3]), "revenue": float(r[4])} for r in top_rows]

        # Store-level alerts (real)
        alerts = []
        # Check overall sell-through
        total_sold = sum(c["sold"] for c in categories)
        total_purch = sum(c["purchased"] for c in categories)
        total_st = min(round(100 * total_sold / total_purch, 1), 100) if total_purch else 0
        total_inv = sum(c["inventory"] for c in categories)
        avg_discount = round(sum(c["discountRate"] * c["sold"] for c in categories if c["sold"] > 0) / max(total_sold, 1), 1)

        if total_st < 50:
            alerts.append({"severity": "high", "message": f"Low sell-through ({total_st}%) — consider markdowns or transfers"})
        elif total_st < 65:
            alerts.append({"severity": "medium", "message": f"Below-average sell-through ({total_st}%)"})
        if avg_discount > 25:
            alerts.append({"severity": "medium", "message": f"High average discount ({avg_discount}%) — margin pressure"})
        # Categories with zero sell-through but inventory
        zero_cats = [c["category"] for c in categories if c["sellThrough"] == 0 and c["inventory"] > 100]
        if zero_cats:
            alerts.append({"severity": "high", "message": f"No sales in: {', '.join(zero_cats[:3])}"})

        return {
            "siteCode": site_code,
            "categories": categories,
            "topProducts": top_products,
            "alerts": alerts,
            "summary": {
                "totalSold": total_sold,
                "totalPurchased": total_purch,
                "totalInventory": total_inv,
                "sellThrough": total_st,
                "avgDiscount": avg_discount,
            }
        }
    finally:
        conn.close()


@router.get("/stores/{site_code}/unclassified-products")
def get_store_unclassified_products(site_code: str, days: int = Query(30, description="Last N days")):
    """List barcodes/SKUs that contributed to Unclassified category sales at this store (orphan or no dept/section)."""
    conn = get_conn()
    try:
        df, df_params = _date_filter(days, "f")
        sql = f"""
            SELECT f.barcode,
                   p.sku_code,
                   COALESCE(p.department, p.section) AS product_dept_section,
                   SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END) AS sold,
                   SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END) AS revenue
            FROM fact_sales f
            LEFT JOIN dim_products p ON f.barcode = p.barcode
            WHERE f.site_code = ?
              AND (p.barcode IS NULL OR (TRIM(COALESCE(p.department, '')) = '' AND TRIM(COALESCE(p.section, '')) = ''))
              {df}
            GROUP BY f.barcode, p.sku_code, 3
            HAVING SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END) > 0
            ORDER BY sold DESC
        """
        rows = query_all(conn, sql, [site_code] + df_params)
        return {
            "siteCode": site_code,
            "days": days,
            "products": [
                {
                    "barcode": str(r[0]) if r[0] is not None else None,
                    "skuCode": r[1],
                    "productDeptSection": r[2],
                    "sold": int(r[3]),
                    "revenue": round(float(r[4] or 0), 2),
                }
                for r in rows
            ],
        }
    finally:
        conn.close()


@router.get("/stores/regions")
def get_store_regions():
    """List of regions for filter dropdown."""
    conn = get_conn()
    try:
        rows = query_all(conn, "SELECT DISTINCT zone FROM dim_stores WHERE zone IS NOT NULL AND TRIM(zone) != '' ORDER BY zone")
        zones = [r[0] for r in rows if r[0]]
        return {"regions": ["All Regions"] + zones}
    finally:
        conn.close()
