"""SKU Transfers API — track inter-store inventory movements."""
from fastapi import APIRouter, Query
from api.db import get_conn, query_all, query_one
from datetime import date, timedelta

router = APIRouter()


@router.get("/transfers/summary")
def get_transfers_summary(days: int = Query(30)):
    """Get transfer summary for recent period."""
    conn = get_conn()
    try:
        end_date = date.today()
        start_date = end_date - timedelta(days=days)
        
        summary = query_one(conn, """
            SELECT 
                COUNT(*) as total_transfers,
                SUM(quantity) as total_quantity,
                COUNT(DISTINCT destination_site_code) as receiving_stores,
                COUNT(DISTINCT sku) as unique_skus
            FROM fact_sku_transfers
            WHERE transfer_date >= ? AND transfer_date <= ?
        """, [start_date, end_date])
        
        if not summary or summary[0] is None:
            return {"totalTransfers": 0, "totalQuantity": 0, "receivingStores": 0, "uniqueSkus": 0, "period": f"Last {days} days"}
        
        return {
            "totalTransfers": int(summary[0] or 0),
            "totalQuantity": int(summary[1] or 0),
            "receivingStores": int(summary[2] or 0),
            "uniqueSkus": int(summary[3] or 0),
            "period": f"Last {days} days"
        }
    finally:
        conn.close()


@router.get("/transfers/by-store")
def get_transfers_by_store(region: str = Query("All Regions"), limit: int = Query(50)):
    """Get top receiving stores."""
    conn = get_conn()
    try:
        if region == "All Regions":
            sql = """
                SELECT 
                    t.destination_site_code,
                    s.site_name,
                    s.zone,
                    COUNT(*) as transfer_count,
                    SUM(t.quantity) as total_quantity,
                    COUNT(DISTINCT t.sku) as unique_skus,
                    MAX(t.transfer_date) as last_transfer
                FROM fact_sku_transfers t
                LEFT JOIN dim_stores s ON t.destination_site_code = s.site_code
                GROUP BY t.destination_site_code, s.site_name, s.zone
                ORDER BY COUNT(*) DESC
                LIMIT ?
            """
            params = [limit]
        else:
            sql = """
                SELECT 
                    t.destination_site_code,
                    s.site_name,
                    s.zone,
                    COUNT(*) as transfer_count,
                    SUM(t.quantity) as total_quantity,
                    COUNT(DISTINCT t.sku) as unique_skus,
                    MAX(t.transfer_date) as last_transfer
                FROM fact_sku_transfers t
                LEFT JOIN dim_stores s ON t.destination_site_code = s.site_code
                WHERE s.zone = ?
                GROUP BY t.destination_site_code, s.site_name, s.zone
                ORDER BY COUNT(*) DESC
                LIMIT ?
            """
            params = [region, limit]
        
        rows = query_all(conn, sql, params)
        
        stores = []
        for r in rows:
            site_code, site_name, zone, count, qty, skus, last_date = r
            stores.append({
                "siteCode": site_code,
                "siteName": site_name or "Unknown",
                "zone": zone or "Unclassified",
                "transferCount": int(count),
                "totalQuantity": int(qty or 0),
                "uniqueSkus": int(skus),
                "lastTransfer": str(last_date) if last_date else None
            })
        
        return {"stores": stores}
    finally:
        conn.close()


@router.get("/transfers/trend")
def get_transfers_trend(months: int = Query(6)):
    """Get monthly transfer trend."""
    conn = get_conn()
    try:
        rows = query_all(conn, """
            SELECT 
                strftime('%Y-%m', transfer_date) as month,
                COUNT(*) as transfer_count,
                SUM(quantity) as total_quantity,
                COUNT(DISTINCT destination_site_code) as active_stores
            FROM fact_sku_transfers
            WHERE transfer_date >= date_trunc('month', CURRENT_DATE - INTERVAL ? MONTH)
            GROUP BY strftime('%Y-%m', transfer_date)
            ORDER BY month
        """, [months])
        
        trend = []
        for month, count, qty, stores in rows:
            trend.append({
                "month": month,
                "transferCount": int(count),
                "totalQuantity": int(qty or 0),
                "activeStores": int(stores)
            })
        
        return {"trend": trend}
    finally:
        conn.close()


@router.get("/transfers/by-sku")
def get_transfers_by_sku(limit: int = Query(20)):
    """Get most transferred SKUs."""
    conn = get_conn()
    try:
        rows = query_all(conn, """
            SELECT 
                t.sku,
                t.barcode,
                p.division,
                p.section,
                p.season,
                COUNT(*) as transfer_count,
                SUM(t.quantity) as total_quantity,
                COUNT(DISTINCT t.destination_site_code) as receiving_stores
            FROM fact_sku_transfers t
            LEFT JOIN dim_products p ON t.barcode = p.barcode
            WHERE t.sku IS NOT NULL AND t.sku != ''
            GROUP BY t.sku, t.barcode, p.division, p.section, p.season
            ORDER BY COUNT(*) DESC
            LIMIT ?
        """, [limit])
        
        skus = []
        for sku, barcode, division, section, season, count, qty, stores in rows:
            skus.append({
                "sku": sku,
                "barcode": barcode,
                "division": division,
                "section": section,
                "season": season,
                "transferCount": int(count),
                "totalQuantity": int(qty or 0),
                "receivingStores": int(stores)
            })
        
        return {"skus": skus}
    finally:
        conn.close()
