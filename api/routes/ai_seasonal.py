from fastapi import APIRouter, Query, HTTPException
from api import db_fabric
import logging

router = APIRouter(prefix="/api/ai-seasonal", tags=["AI Seasonal Planning"])
logger = logging.getLogger(__name__)

def build_ai_where_clause(site=None, sku=None, dept=None, month=None):
    conditions = []
    if site and site != 'All Sites': conditions.append(f"Site = '{site}'")
    if sku and sku != 'All SKUs': conditions.append(f"SKU = '{sku}'")
    if dept and dept != 'All Departments': conditions.append(f"DEPARTMENT = '{dept}'")
    if month and month != 'All Months': conditions.append(f"Month = {month}") # Month is numeric
    
    if conditions:
        return "WHERE " + " AND ".join(conditions)
    return ""

@router.get("/recommendation-summary")
async def get_recommendation_summary(
    target_season: str = None,
    baseline_years: list[str] = Query(default=[]),
    site: str = None,
    sku: str = None,
    dept: str = None,
    month: str = None
):
    """
    Fetch the sum of predicted_monthly_qty_2026 and calculate Growth % vs baseline years.
    """
    try:
        # User requirement: Qty growth % should only be calculated based on 2025 sold qty
        baseline_sum_expr = "SUM(sold_qty_2025)"

        where_clause = build_ai_where_clause(site, sku, dept, month)

        query = f"""
            SELECT 
                SUM(predicted_monthly_qty_2026) as total_recommended_units,
                {baseline_sum_expr} as baseline_qty
            FROM ss26_planning_demo
            {where_clause}
        """
        
        result = db_fabric.query_all(query)
        
        total_units = 0
        baseline_qty = 0
        if result and len(result) > 0:
            total_units = float(result[0][0] or 0)
            baseline_qty = float(result[0][1] or 0)
            
        growth_pct = 0
        if baseline_qty > 0:
            growth_pct = ((total_units - baseline_qty) / baseline_qty) * 100
            
        return {
            "totalRecommendedUnits": int(total_units),
            "qtyGrowthPct": round(growth_pct, 1),
            "targetSeason": target_season,
            "baselineYears": baseline_years,
            "status": "success"
        }
    except Exception as e:
        logger.error(f"Error fetching AI Recommendation summary: {e}")
        return {
            "totalRecommendedUnits": 0, "qtyGrowthPct": 0, "error": str(e), "status": "error"
        }

@router.get("/attribute-allocation")
async def get_attribute_allocation(
    attribute: str = "category",
    baseline_years: list[str] = Query(default=[]),
    site: str = None,
    sku: str = None,
    dept: str = None,
    month: str = None
):
    """
    Fetch comparison between groups (Category, Color, Fabric) for individual baseline years vs target predicted qty.
    """
    try:
        # Map frontend attribute names to database column names
        attr_map = {
            "category": "DEPARTMENT",
            "color": "Color",
            "fabric": "Fabric"
        }
        db_attr = attr_map.get(attribute.lower(), "DEPARTMENT")
        
        where_clause = build_ai_where_clause(site, sku, dept, month)
        query = f"""
            SELECT 
                {db_attr},
                SUM(sold_qty_2024) as sold_2024,
                SUM(sold_qty_2025) as sold_2025,
                SUM(predicted_monthly_qty_2026) as predicted_qty
            FROM ss26_planning_demo
            {where_clause}
            GROUP BY {db_attr}
            ORDER BY predicted_qty DESC
        """
        
        rows = db_fabric.query_all(query)
        data = []
        for r in (rows or []):
            label = str(r[0] or "Unknown")
            if label.lower() == 'none' or label.strip() == '':
                label = "Unknown"

            data.append({
                "label": label,
                "sold_2024": float(r[1] or 0),
                "sold_2025": float(r[2] or 0),
                "predicted": float(r[3] or 0)
            })
            
        return {"data": data, "status": "success"}
    except Exception as e:
        logger.error(f"Error fetching {attribute} allocation: {e}")
        return {"data": [], "error": str(e), "status": "error"}

@router.get("/planning-insights")
async def get_planning_insights(
    limit: int = 50, 
    offset: int = 0,
    site: str = None,
    sku: str = None,
    dept: str = None,
    month: str = None,
    sort_by: str = "predicted_monthly_qty_2026",
    sort_order: str = "DESC"
):
    """
    Fetch raw planning insights from ss26_planning_demo with dynamic sorting and total count.
    """
    try:
        where_clause = build_ai_where_clause(site, sku, dept, month)
        
        # Column mapping for security and convenience
        col_map = {
            "site": "Site", "sku": "SKU", "month": "Month", "department": "DEPARTMENT",
            "color": "Color", "fabric": "Fabric", "section": "SECTION", "occasion": "OCCASION",
            "product_type": "PRODUCT_TYPE", "fit_type": "FIT_TYPE", "type": "TYPE",
            "predicted_qty": "predicted_monthly_qty_2026", "action": "action",
            "risk_flag": "risk_flag", "discount_band": "discount_band", "focus_comment": "focus_comment"
        }
        db_col = col_map.get(sort_by, "predicted_monthly_qty_2026")
        order = "DESC" if sort_order.upper() == "DESC" else "ASC"

        # Query total count
        count_query = f"SELECT COUNT(*) FROM ss26_planning_demo {where_clause}"
        count_res = db_fabric.query_all(count_query)
        total_count = int(count_res[0][0]) if count_res else 0

        # Query data
        query = f"""
            SELECT 
                Site, SKU, Month, DEPARTMENT, Color, Fabric, SECTION, OCCASION, PRODUCT_TYPE, FIT_TYPE, TYPE, 
                predicted_monthly_qty_2026, action, risk_flag, discount_band, focus_comment
            FROM ss26_planning_demo
            {where_clause}
            ORDER BY {db_col} {order}
            OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY
        """
        
        rows = db_fabric.query_all(query)
        data = []
        for r in (rows or []):
            data.append({
                "site": str(r[0] or ""),
                "sku": str(r[1] or ""),
                "month": str(r[2] or ""),
                "department": str(r[3] or ""),
                "color": str(r[4] or ""),
                "fabric": str(r[5] or ""),
                "section": str(r[6] or ""),
                "occasion": str(r[7] or ""),
                "product_type": str(r[8] or ""),
                "fit_type": str(r[9] or ""),
                "type": str(r[10] or ""),
                "predicted_qty": float(r[11] or 0),
                "action": str(r[12] or ""),
                "risk_flag": str(r[13] or ""),
                "discount_band": str(r[14] or ""),
                "focus_comment": str(r[15] or "")
            })
            
        return {"data": data, "total": total_count, "status": "success"}
    except Exception as e:
        logger.error(f"Error fetching planning insights: {e}")
        return {"data": [], "total": 0, "error": str(e), "status": "error"}

@router.get("/sites")
async def get_sites():
    rows = db_fabric.query_all("SELECT DISTINCT Site FROM ss26_planning_demo WHERE Site IS NOT NULL ORDER BY Site")
    return ["All Sites"] + [r[0] for r in rows]

@router.get("/skus")
async def get_skus():
    rows = db_fabric.query_all("SELECT DISTINCT SKU FROM ss26_planning_demo WHERE SKU IS NOT NULL ORDER BY SKU")
    return ["All SKUs"] + [r[0] for r in rows]

@router.get("/departments")
async def get_departments():
    rows = db_fabric.query_all("SELECT DISTINCT DEPARTMENT FROM ss26_planning_demo WHERE DEPARTMENT IS NOT NULL ORDER BY DEPARTMENT")
    return ["All Departments"] + [r[0] for r in rows]

@router.get("/months")
async def get_months():
    month_map = {
        1: "January", 2: "February", 3: "March", 4: "April", 5: "May", 6: "June",
        7: "July", 8: "August", 9: "September", 10: "October", 11: "November", 12: "December"
    }
    rows = db_fabric.query_all("SELECT DISTINCT Month FROM ss26_planning_demo WHERE Month IS NOT NULL ORDER BY Month")
    return [{"id": "All Months", "label": "All Months"}] + [
        {"id": str(r[0]), "label": month_map.get(int(r[0]), str(r[0]))} for r in rows
    ]
