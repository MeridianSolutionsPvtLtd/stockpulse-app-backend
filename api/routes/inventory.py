"""Inventory API — real stock health metrics from DuckDB."""
from fastapi import APIRouter, Query
from api.db import get_conn, query_all
from api.config import DATABASE_BACKEND

router = APIRouter()
_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"

def _fetch_inventory_dax(season: str, days: int, region: str = "All Regions", sku: str = "All SKUs", category: str = "All Categories") -> dict:
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, C_SALE_DATE, C_SALE_AMOUNT, C_SALE_IS_RETURN, C_SALE_QTY, C_SALE_DISCOUNT, T_FI, C_INV_QTY, C_INV_BARCODE, C_INV_SITE, C_SALE_BARCODE, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    d = max(int(days), 1)
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    
    # Region filters for DAX (Season filter removed per user request)
    dax_filters = [
        "KEEPFILTERS(FILTER(ALL('Date_Table'[Month-Year]), 'Date_Table'[Month-Year] = LatestMonthYear))"
    ]
    if region and region != "All Regions":
        # Adjust table/column for region if needed; using Site from sales here
        dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Fact_Sales_Detail'[Site]), 'Fact_Sales_Detail'[Site] = \"{region}\"))")
    
    if sku and sku != "All SKUs":
        dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Fact_Sales_Detail'[SKU]), 'Fact_Sales_Detail'[SKU] = \"{sku}\"))")
    
    if category and category != "All Categories":
        dax_filters.append(f"KEEPFILTERS(FILTER(ALL('Fact_Sales_Detail'[Department]), 'Fact_Sales_Detail'[Department] = \"{category}\"))")
    
    filters_str = ",\n        ".join(dax_filters)

    try:
        # 1. Fetch Categories using SUMMARIZECOLUMNS
        q_cat = f"""
EVALUATE
VAR MaxDateVal = MAXX('Fact_Sales_Detail', 'Fact_Sales_Detail'[Date])
VAR LatestMonthYear = CALCULATE(MAX('Date_Table'[Month-Year]), 'Date_Table'[Date] = MaxDateVal)
RETURN
SUMMARIZECOLUMNS(
    'Fact_Sales_Detail'[Department],
    'Dim_SKU_Store_Season'[SKU],
    {filters_str},
    "received", [Qty_Received],
    "sold", [Qty_Sold],
    "revenue", [Sales_Amt],
    "avg_discount", [1Discount %],
    "sell_through", [Sell Through %],
    "current_stock", [Qty_Received] - [Qty_Sold],
    "days_of_supply", [Days_of_Supply]
)
"""
        print(f"DEBUG INVENTORY: q_cat query: {q_cat}")
        rows = execute_dax(q_cat)
        categories = []
        
        for r in (rows or []):
            rn = _norm_row(r)
            cat = rn.get("DEPARTMENT") or rn.get("Fact_Sales_Detail[DEPARTMENT]") or "Unclassified"
            sku = rn.get("SKU") or rn.get("Dim_SKU_Store_Season[SKU]") or "N/A"
            sold = float(rn.get("sold") or 0)
            received = float(rn.get("received") or 0)
            revenue = float(rn.get("revenue") or 0)
            discount = round(float(rn.get("avg_discount") or 0) * 100, 1)
            st_val = float(rn.get("sell_through") or 0)
            st_pct = min(round(st_val * 100, 1), 100.0) if st_val <= 1.0 else min(round(st_val, 1), 100.0)
            
            stock_units = max(float(rn.get("current_stock") or 0), 0)
            stock_value = revenue * 0.6 
            dos = float(rn.get("days_of_supply") or rn.get("DAYS_OF_SUPPLY") or 0)
            
            health = "Healthy"
            if sold == 0: health = "No Sales"
            elif st_pct > 50: health = "Fast Moving"
            elif 20 <= st_pct <= 30: health = "Slow Moving"
            elif st_pct < 20: health = "At Risk"
            else: health = "Moderate"

            categories.append({
                "category": cat,
                "sku": sku,
                "skuCount": 1,
                "storeCount": 495,
                "stockUnits": int(stock_units),
                "stockValue": stock_value,
                "unitsSold": int(sold),
                "revenue": revenue,
                "soldSkuCount": 1 if sold > 0 else 0,
                "avgDiscount": discount,
                "unitsPurchased": int(received),
                "daysOfSupply": int(dos) if dos < 9999 else 999,
                "dailyVelocity": round(sold / d, 1) if d > 0 else 0,
                "sellThrough": st_pct,
                "health": health
            })

        
        # 2. Fetch Summary Health using requested measures
        # Use SUMMARIZECOLUMNS to respect the region_filter
        q_sum = f"""
EVALUATE
VAR MaxDateVal = MAXX('Fact_Sales_Detail', 'Fact_Sales_Detail'[Date])
VAR LatestMonthYear = CALCULATE(MAX('Date_Table'[Month-Year]), 'Date_Table'[Date] = MaxDateVal)
RETURN
SUMMARIZECOLUMNS(
    {filters_str},
    "total", DISTINCTCOUNT('Dim_SKU_Store_Season'[SKU]),
    "fast", [Fast_Moving_SKUs],
    "slow", [Slow_Moving_SKUs],
    "risk", [At_Risk_SKUs_ST]
)
"""
        print(f"DEBUG INVENTORY: q_sum query: {q_sum}")
        sum_rows = execute_dax(q_sum)
        summary = {"totalSkus": 0, "fastMovingSkus": 0, "slowMovingSkus": 0, "atRiskSkus": 0}
        if sum_rows:
            sr = _norm_row(sum_rows[0])
            summary = {
                "totalSkus": int(float(sr.get("total") or 0)),
                "fastMovingSkus": int(float(sr.get("fast") or 0)),
                "slowMovingSkus": int(float(sr.get("slow") or 0)),
                "atRiskSkus": int(float(sr.get("risk") or 0))
            }
            
        print(f"DEBUG INVENTORY: Query Succeeded. Returned {len(rows)} category rows and {len(sum_rows) if sum_rows else 0} summary rows.")
        return {
            "summary": summary, 
            "categories": sorted(categories, key=lambda x: -x["stockValue"]),
            "data_source": "powerbi"
        }
        
    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        print(f"DEBUG INVENTORY ERROR: {e}\n{err_detail}")
        empty_summary = {"totalSkus": 0, "fastMovingSkus": 0, "slowMovingSkus": 0, "atRiskSkus": 0}
        return {"summary": empty_summary, "categories": [], "error": str(e), "detail": err_detail}



@router.get("/inventory")
def get_inventory(region: str = Query("All Regions"), season: str = Query("All Seasons"), sku: str = Query("All SKUs"), category: str = Query("All Categories"), days: int = Query(30)):
    """Inventory overview: KPI summary + category-level metrics with real DOS."""
    from api.config import DATABASE_BACKEND
    print(f"DEBUG: get_inventory using BACKEND: {DATABASE_BACKEND}")
    if (DATABASE_BACKEND or "").lower() == "powerbi":
        return _fetch_inventory_dax(season, days, region, sku, category)

    
    conn = get_conn()
    try:
        d = 30 # Force 30 days for Inventory Health
        region_join = ""
        region_where = ""
        region_params: list = []
        if region != "All Regions":
            region_join = "JOIN dim_stores st ON i.site_code = st.site_code"
            region_where = "AND st.zone = ?"
            region_params = [region]

        # Category-level: stock, value, sold, revenue, velocity, DOS
        sql = f"""
            WITH source_totals AS (
                SELECT i.site_code, i.barcode, i.source, SUM(i.quantity) as quantity
                FROM fact_inventory_snapshots i
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.quantity > 0
                GROUP BY i.site_code, i.barcode, i.source
            ),
            unique_inventory AS (
                SELECT i.site_code, 
                       i.barcode, 
                       MAX(i.quantity) as quantity
                FROM source_totals i
                GROUP BY i.site_code, i.barcode
            ),
            cat_stock AS (
                SELECT COALESCE(p.department, p.section, 'Unclassified') AS category,
                       COUNT(DISTINCT i.barcode) AS sku_count,
                       COUNT(DISTINCT i.site_code) AS store_count,
                       SUM(i.quantity) AS stock_units,
                       SUM(i.quantity * COALESCE(p.mrp, 0)) AS stock_value
                FROM unique_inventory i
                LEFT JOIN dim_products p ON i.barcode = p.barcode
                {region_join}
                WHERE 1=1
                  {region_where}
                GROUP BY 1
            ),
            cat_sold AS (
                SELECT COALESCE(p.department, p.section, 'Unclassified') AS category,
                       COUNT(DISTINCT f.barcode) AS sold_sku_count,
                       SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END) AS units_sold,
                       SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END) AS revenue,
                       AVG(CASE WHEN NOT f.is_return AND f.discount_pct BETWEEN 0 AND 80
                           THEN f.discount_pct END) AS avg_discount
                FROM fact_sales f
                LEFT JOIN dim_products p ON f.barcode = p.barcode
                {"JOIN dim_stores st2 ON f.site_code = st2.site_code" if region != "All Regions" else ""}
                WHERE f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY
                  {"AND st2.zone = ?" if region != "All Regions" else ""}
                GROUP BY 1
            ),
            cat_purchased AS (
                SELECT COALESCE(p.department, p.section, 'Unclassified') AS category,
                       SUM(fp.quantity) AS units_purchased
                FROM fact_purchases fp
                LEFT JOIN dim_products p ON fp.barcode = p.barcode
                {"JOIN dim_stores st3 ON fp.site_code = st3.site_code" if region != "All Regions" else ""}
                WHERE fp.purchase_date >= (SELECT MAX(purchase_date) FROM fact_purchases) - INTERVAL '{d}' DAY
                  {"AND st3.zone = ?" if region != "All Regions" else ""}
                GROUP BY 1
            )
            SELECT cs.category,
                   cs.sku_count,
                   cs.store_count,
                   cs.stock_units,
                   cs.stock_value,
                   COALESCE(cl.units_sold, 0) AS units_sold,
                   COALESCE(cl.revenue, 0) AS revenue,
                   COALESCE(cl.sold_sku_count, 0) AS sold_sku_count,
                   COALESCE(cl.avg_discount, 0) AS avg_discount,
                   COALESCE(cp.units_purchased, 0) AS units_purchased,
                   CASE WHEN COALESCE(cl.units_sold, 0) > 0
                        THEN ROUND(cs.stock_units * {d}.0 / cl.units_sold, 0)
                        ELSE 9999 END AS days_of_supply,
                   CASE WHEN COALESCE(cl.units_sold, 0) > 0
                        THEN ROUND(cl.units_sold * 1.0 / {d}, 1)
                        ELSE 0 END AS daily_velocity
            FROM cat_stock cs
            LEFT JOIN cat_sold cl ON cs.category = cl.category
            LEFT JOIN cat_purchased cp ON cs.category = cp.category
            ORDER BY cs.stock_value DESC
        """
        params = list(region_params)
        if region != "All Regions":
            params += [region]  # for cat_sold
            params += [region]  # for cat_purchased

        rows = query_all(conn, sql, params)

        categories = []
        total_stock = 0
        total_value = 0.0
        total_sold = 0
        total_revenue = 0.0
        total_purchased = 0
        total_skus = 0
        total_sold_skus = 0
        needs_replenish = 0
        overstock_units = 0
        slow_moving_skus = 0

        for r in rows:
            cat, sku_count, store_count, stock, value, sold, rev, sold_skus, avg_disc, purchased, dos, velocity = r
            stock = int(stock or 0)
            value = float(value or 0)
            sold = int(sold or 0)
            rev = float(rev or 0)
            sold_skus = int(sold_skus or 0)
            purchased = int(purchased or 0)
            dos = int(dos) if dos and dos < 9999 else None
            velocity = float(velocity or 0)
            sku_count = int(sku_count or 0)
            store_count = int(store_count or 0)
            avg_disc = round(float(avg_disc or 0), 1)

            # Health status
            if dos is None:
                health = "No Sales"
                slow_moving_skus += sku_count - sold_skus
            elif dos <= 7:
                health = "Critical"
                needs_replenish += 1
            elif dos <= 21:
                health = "Low"
                needs_replenish += 1
            elif dos <= 90:
                health = "Healthy"
            else:
                health = "Overstock"
                overstock_units += stock

            # SKUs with no sales in period
            no_sale_skus = max(sku_count - sold_skus, 0)
            if dos is not None:
                slow_moving_skus += no_sale_skus

            if purchased > 0:
                sell_through = min(round(100 * sold / purchased, 1), 100)
            elif sold + stock > 0:
                sell_through = round(100 * sold / (sold + stock), 1)
            else:
                sell_through = 0

            total_stock += stock
            total_value += value
            total_sold += sold
            total_revenue += rev
            total_purchased += purchased
            total_skus += sku_count
            total_sold_skus += sold_skus

            categories.append({
                "category": cat or "Unclassified",
                "skuCount": sku_count,
                "storeCount": store_count,
                "stockUnits": stock,
                "stockValue": value,
                "unitsSold": sold,
                "revenue": rev,
                "unitsPurchased": purchased,
                "sellThrough": sell_through,
                "avgDiscount": avg_disc,
                "daysOfSupply": dos,
                "dailyVelocity": velocity,
                "health": health,
                "soldSkuCount": sold_skus,
                "noSaleSkus": no_sale_skus,
            })

        # Compute overall KPIs
        overall_dos = round(total_stock * d / max(total_sold, 1))
        overall_velocity = round(total_sold / max(d, 1), 1)
        overall_sell_through = min(round(100 * total_sold / max(total_purchased, 1), 1), 100) if total_purchased > 0 else 0
        no_sale_total = max(total_skus - total_sold_skus, 0)

        # Health distribution
        health_dist = {}
        for c in categories:
            h = c["health"]
            health_dist[h] = health_dist.get(h, 0) + 1

        # SKU-level health metrics for cards
        sku_sql = f"""
            WITH source_totals AS (
                SELECT i.site_code, i.barcode, i.source, SUM(i.quantity) as quantity
                FROM fact_inventory_snapshots i
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.quantity > 0
                GROUP BY i.site_code, i.barcode, i.source
            ),
            unique_inventory AS (
                SELECT i.site_code, i.barcode, MAX(i.quantity) as quantity
                FROM source_totals i
                GROUP BY i.site_code, i.barcode
            ),
            sku_agg AS (
                SELECT i.barcode,
                       SUM(i.quantity) AS stock,
                       COALESCE(SUM(s.quantity), 0) AS sold
                FROM unique_inventory i
                LEFT JOIN (
                    SELECT f.barcode, f.site_code, SUM(f.quantity) AS quantity
                    FROM fact_sales f
                    WHERE f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY
                      AND NOT f.is_return
                    GROUP BY 1, 2
                ) s ON i.barcode = s.barcode AND i.site_code = s.site_code
                {region_join}
                WHERE 1=1
                  {region_where}
                GROUP BY 1
            ),
            sku_metrics AS (
                SELECT barcode,
                       stock,
                       sold,
                       CASE WHEN sold + stock > 0 THEN sold * 1.0 / (sold + stock) ELSE 0 END AS sell_through
                FROM sku_agg
            )
            SELECT
                COUNT(*) AS total_skus,
                COUNT(CASE WHEN sell_through > 0.5 THEN 1 END) AS fast_moving,
                COUNT(CASE WHEN sell_through >= 0.2 AND sell_through <= 0.3 THEN 1 END) AS slow_moving,
                COUNT(CASE WHEN sell_through < 0.2 THEN 1 END) AS at_risk
            FROM sku_metrics
        """
        sku_counts = query_all(conn, sku_sql, region_params + region_params if region != "All Regions" else [])
        
        # Default counts
        c_total, c_fast, c_slow, c_risk = 0, 0, 0, 0
        if sku_counts and sku_counts[0]:
            c_total, c_fast, c_slow, c_risk = sku_counts[0]

        summary = {
            "totalStockUnits": total_stock,
            "totalStockValue": total_value,
            "totalUnitsSold": total_sold,
            "totalRevenue": total_revenue,
            "totalUnitsPurchased": total_purchased,
            "totalSkus": int(c_total or total_skus), # Use SKU level count if successful
            "soldSkus": total_sold_skus,
            "noSaleSkus": no_sale_total,
            "overallDaysOfSupply": overall_dos,
            "dailyVelocity": overall_velocity,
            "sellThrough": overall_sell_through,
            "needsReplenishCategories": needs_replenish,
            "overstockUnits": overstock_units,
            "healthDistribution": health_dist,
            "categoryCount": len(categories),
            # New metrics for cards
            "fastMovingSkus": int(c_fast or 0),
            "slowMovingSkus": int(c_slow or 0),
            "atRiskSkus": int(c_risk or 0),
        }

        return {
            "summary": summary, 
            "categories": categories,
            "data_source": "duckdb"
        }
    finally:
        conn.close()


@router.get("/inventory/{category}/skus")
def get_category_skus(category: str, region: str = Query("All Regions"), days: int = Query(30)):
    """SKU-level breakdown for a given category with stock, sales, DOS, size."""
    conn = get_conn()
    try:
        d = 30 # Force 30 days for Inventory Health
        region_join_inv = ""
        region_where_inv = ""
        region_join_sales = ""
        region_where_sales = ""
        params: list = []
        sale_params: list = []

        if region != "All Regions":
            region_join_inv = "JOIN dim_stores st ON i.site_code = st.site_code"
            region_where_inv = "AND st.zone = ?"
            params = [region]
            region_join_sales = "JOIN dim_stores st2 ON f.site_code = st2.site_code"
            region_where_sales = "AND st2.zone = ?"
            sale_params = [region]

        sql = f"""
            WITH source_totals AS (
                SELECT i.site_code, i.barcode, i.source, SUM(i.quantity) as quantity
                FROM fact_inventory_snapshots i
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.quantity > 0
                GROUP BY i.site_code, i.barcode, i.source
            ),
            unique_inventory AS (
                SELECT i.site_code, i.barcode, MAX(i.quantity) as quantity
                FROM source_totals i
                GROUP BY i.site_code, i.barcode
            ),
            sku_stock AS (
                SELECT i.barcode,
                       SUM(i.quantity) AS stock,
                       COUNT(DISTINCT i.site_code) AS store_count
                FROM unique_inventory i
                LEFT JOIN dim_products p ON i.barcode = p.barcode
                {region_join_inv}
                WHERE COALESCE(p.department, p.section, 'Unclassified') = ?
                  {region_where_inv}
                GROUP BY i.barcode
            ),
            sku_sold AS (
                SELECT f.barcode,
                       SUM(CASE WHEN NOT f.is_return THEN f.quantity ELSE 0 END) AS sold,
                       SUM(CASE WHEN NOT f.is_return THEN f.net_amount ELSE 0 END) AS revenue
                FROM fact_sales f
                LEFT JOIN dim_products p ON f.barcode = p.barcode
                {region_join_sales}
                WHERE f.sale_date >= (SELECT MAX(sale_date) FROM fact_sales) - INTERVAL '{d}' DAY
                  AND COALESCE(p.department, p.section, 'Unclassified') = ?
                  {region_where_sales}
                GROUP BY f.barcode
            ),
            last_sale AS (
                SELECT f.barcode, MAX(f.sale_date) AS last_sale_date
                FROM fact_sales f
                LEFT JOIN dim_products p ON f.barcode = p.barcode
                WHERE NOT f.is_return AND f.quantity > 0
                  AND COALESCE(p.department, p.section, 'Unclassified') = ?
                GROUP BY f.barcode
            )
            SELECT ss.barcode,
                   p.sku_code,
                   COALESCE(p.size, 'N/A') AS size,
                   COALESCE(p.mrp, 0) AS mrp,
                   ss.stock,
                   ss.stock * COALESCE(p.mrp, 2000) AS stock_value,
                   ss.store_count,
                   COALESCE(sl.sold, 0) AS sold,
                   COALESCE(sl.revenue, 0) AS revenue,
                   CASE WHEN COALESCE(sl.sold, 0) > 0
                        THEN ROUND(ss.stock * {d}.0 / sl.sold, 0)
                        ELSE NULL END AS dos,
                   CASE WHEN COALESCE(sl.sold, 0) > 0
                        THEN ROUND(sl.sold * 1.0 / {d}, 2)
                        ELSE 0 END AS velocity,
                   strftime(ls.last_sale_date, '%Y-%m-%d') AS last_sale
            FROM sku_stock ss
            LEFT JOIN dim_products p ON ss.barcode = p.barcode
            LEFT JOIN sku_sold sl ON ss.barcode = sl.barcode
            LEFT JOIN last_sale ls ON ss.barcode = ls.barcode
            ORDER BY ss.stock DESC
        """
        all_params = [category] + params + [category] + sale_params + [category]
        rows = query_all(conn, sql, all_params)

        # Collect barcodes from results to fetch store-level stock
        barcodes = [str(r[0]) for r in rows if r[0]]
        store_map: dict = {}  # barcode -> [{siteCode, storeName, qty}]
        if barcodes:
            placeholders = ",".join(["?" for _ in barcodes])
            store_sql = f"""
                SELECT i.barcode, i.site_code, s.site_name, i.quantity
                FROM fact_inventory_snapshots i
                LEFT JOIN dim_stores s ON i.site_code = s.site_code
                WHERE i.snapshot_date = (SELECT MAX(snapshot_date) FROM fact_inventory_snapshots)
                  AND i.quantity > 0
                  AND i.barcode IN ({placeholders})
                ORDER BY i.barcode, i.quantity DESC
            """
            store_rows = query_all(conn, store_sql, barcodes)
            for sr in store_rows:
                bc = str(sr[0])
                if bc not in store_map:
                    store_map[bc] = []
                store_map[bc].append({
                    "siteCode": str(sr[1]),
                    "storeName": ((sr[2] or str(sr[1])).strip()),
                    "quantity": int(sr[3] or 0),
                })

        skus = []
        for r in rows:
            barcode, sku, size, mrp, stock, stock_val, stores, sold, rev, dos, velocity, last_sale = r
            barcode_str = str(barcode)
            stock = int(stock or 0)
            sold = int(sold or 0)
            dos_val = int(dos) if dos is not None else None

            if dos_val is None:
                health = "No Sales"
            elif dos_val <= 7:
                health = "Critical"
            elif dos_val <= 21:
                health = "Low"
            elif dos_val <= 90:
                health = "Healthy"
            else:
                health = "Overstock"

            sku_stores = store_map.get(barcode_str, [])

            skus.append({
                "barcode": barcode_str,
                "sku": sku or barcode_str,
                "size": size or "N/A",
                "mrp": float(mrp or 0),
                "stockUnits": stock,
                "stockValue": float(stock_val or 0),
                "storeCount": int(stores or 0),
                "unitsSold": sold,
                "revenue": float(rev or 0),
                "daysOfSupply": dos_val,
                "dailyVelocity": float(velocity or 0),
                "lastSaleDate": last_sale,
                "health": health,
                "stores": sku_stores,
            })

        return {"category": category, "skus": skus, "totalSkus": len(skus)}
    finally:
        conn.close()
