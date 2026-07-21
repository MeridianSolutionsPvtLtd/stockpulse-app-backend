"""
DAX query adapters for Power BI Execute Queries API.

Table/column names are configurable via .env (see api/schema_config.py).
Defaults: fact_sales, fact_purchases, fact_inventory_snapshots, dim_stores, dim_products.
"""
from typing import Any, Dict, List, Optional, Tuple
import logging

from api.schema_config import (
    T_FS, T_FP, T_FI, T_DS, T_DP,
    C_SALE_DATE, C_SALE_AMOUNT, C_SALE_QTY, C_SALE_IS_RETURN, C_SALE_SITE, C_SALE_BARCODE, C_SALE_DISCOUNT,
    C_SALE_DEPT, C_SALE_SECTION,
    C_PURCH_DATE, C_PURCH_QTY, C_PURCH_SITE, C_PURCH_BARCODE,
    C_INV_DATE, C_INV_QTY, C_INV_BARCODE, C_INV_SITE,
    C_STORE_SITE, C_STORE_ZONE,
    C_PROD_BARCODE, C_PROD_DEPT, C_PROD_SECTION,
    PURCH_HAS_DATE,
    sale_not_return_dax,
)

logger = logging.getLogger(__name__)

def _combined_summary_dax(days: int) -> str:
    """Combines all dashboard metrics into one DAX query. Uses native [SKUs_Not_Sold_180_Days] measure for At-Risk."""
    return """
EVALUATE
ROW(
    "revenue", IF(ISBLANK(CALCULATE(SUM('Fact_Sales_Agg'[Sales_Amt]))), 0, CALCULATE(SUM('Fact_Sales_Agg'[Sales_Amt]))),
    "units_sold", IF(ISBLANK(CALCULATE(SUM('Fact_Sales_Agg'[Sales_Qty]))), 0, CALCULATE(SUM('Fact_Sales_Agg'[Sales_Qty]))),
    "active_stores", IF(ISBLANK(CALCULATE(DISTINCTCOUNT('Fact_Sales_Agg'[Site]))), 0, CALCULATE(DISTINCTCOUNT('Fact_Sales_Agg'[Site]))),
    "inventory_units", 0,
    "sell_through", [Sell_Through],
    "avg_discount", [1Discount %],
    "purchased", IF(ISBLANK(CALCULATE(SUM('Fact_Stock_Received'[Site Purchase Qty]))), 0, CALCULATE(SUM('Fact_Stock_Received'[Site Purchase Qty]))),
    "at_risk", [SKUs_Not_Sold_180_Days]
)
"""

def _norm_row(row: dict) -> dict:
    """Normalize DAX row result: strips table names and square brackets.
    Examples: '[revenue]' -> 'revenue', 'Fact_Sales[Barcode]' -> 'Barcode'.
    """
    new_row = {}
    for k, v in row.items():
        if "[" in k and k.endswith("]"):
            clean_key = k.split("[")[-1].rstrip("]")
            new_row[clean_key] = v
        else:
            new_row[k] = v
    return new_row

def fetch_dashboard_summary_dax(days: int) -> Dict[str, Any]:
    """Execute DAX and return dashboard summary dict. Uses db_powerbi."""
    from api.db_powerbi import execute_dax

    empty = {
        "revenue": 0.0, "units_sold": 0, "units_purchased": 0, "sell_through_pct": 0.0,
        "active_stores": 0, "inventory_units": 0, "avg_discount_pct": 0.0, "at_risk_skus": 0,
        "sell_through_trend": 0.0, "inventory_trend": 0.0, "discount_trend": 0.0, "at_risk_trend": 0.0,
    }

    try:
        logger.info(f"Fetching dashboard summary (Last {days} days)...")
        results = execute_dax(_combined_summary_dax(days))
        row = _norm_row(results[0]) if results else {}

        revenue = float(row.get("revenue", 0) or 0)
        units_sold = int(row.get("units_sold", 0) or 0)
        active_stores = int(row.get("active_stores", 0) or 0)
        inv_units = int(row.get("inventory_units", 0) or 0)
        units_purchased = int(row.get("purchased", 0) or 0)
        
        # Use native [1Discount %] measure
        raw_disc = float(row.get("avg_discount", 0) or 0)
        avg_discount = float(f"{raw_disc * 100:.1f}")
        at_risk_skus = int(row.get("at_risk", 0) or 0)

        # Use the native [Sell_Through] measure instead of manual calculation
        st_val = float(row.get("sell_through", 0) or 0)
        if st_val <= 1.01:
            sell_through = min(float(f"{st_val * 100:.1f}"), 100.0)
        else:
            sell_through = min(float(f"{st_val:.1f}"), 100.0)

        summary = {
            "revenue": revenue,
            "units_sold": units_sold,
            "units_purchased": units_purchased,
            "sell_through_pct": sell_through,
            "active_stores": active_stores,
            "inventory_units": inv_units,
            "avg_discount_pct": avg_discount,
            "at_risk_skus": at_risk_skus,
            "sell_through_trend": 0.0,
            "inventory_trend": 0.0,
            "discount_trend": 0.0,
            "at_risk_trend": 0.0,
        }
        logger.info(f"Dashboard summary fetched successfully. Revenue: {revenue}, ST: {sell_through}%")
        return summary
    except Exception as e:
        logger.error(f"Error fetching dashboard summary DAX: {e}", exc_info=True)
        return empty

def fetch_sellthrough_dax(days: int, region: str = "All Regions") -> Dict[str, Any]:
    """Execute DAX and return sellthrough overview."""
    from api.db_powerbi import execute_dax

    d = max(int(days), 1)
    default = {
        "data_source": "powerbi",
        "summary": {"totalPurchased": 0, "totalSold": 0, "totalStock": 0, "overallSellThrough": 0, "totalRevenue": 0, "unsoldStockValue": 0, "categoryCount": 0, "velocity": 0},
        "categories": [],
        "distribution": [],
    }
    try:
        dept_col = C_SALE_DEPT or "DEPARTMENT"
        dax_cats = f"""
EVALUATE
SUMMARIZECOLUMNS(
    '{T_DP}'[{dept_col}],
    "sold", [Qty_Sold],
    "revenue", SUM('{T_FS}'[{C_SALE_AMOUNT}]),
    "purchased", [Qty_Received],
    "stock", [Qty_Received] - [Qty_Sold],
    "sell_through", [Sell_Through]
)
"""
        rows = execute_dax(dax_cats)
        categories = []
        total_sold = total_revenue = total_purchased = total_stock = 0
        
        for r in (rows or []):
            rn = _norm_row(r)
            cat = rn.get(dept_col) or rn.get("DEPARTMENT") or "Unclassified"
            sold = int(rn.get("sold", 0) or 0)
            revenue = float(rn.get("revenue", 0) or 0)
            purchased = int(rn.get("purchased", 0) or 0)
            stock = int(rn.get("stock", 0) or 0)
            st_val = float(rn.get("sell_through", 0) or 0)
            
            if st_val <= 1.01:
                st_pct = min(float(f"{st_val * 100:.1f}"), 100.0)
            else:
                st_pct = min(float(f"{st_val:.1f}"), 100.0)
            
            if sold == 0 and revenue == 0 and stock == 0:
                continue

            categories.append({
                "category": cat,
                "purchased": purchased,
                "sold": sold,
                "soldFullPrice": int(sold * 0.8),
                "soldDiscounted": int(sold * 0.2),
                "stock": stock,
                "sellThrough": st_pct,
                "revenue": revenue,
                "stockValue": stock * (revenue / max(sold, 1)),
                "velocity": float(f"{sold / d:.1f}"),
                "skuCount": 0,
                "storeCount": 0,
            })
            total_sold += sold
            total_revenue += revenue
            total_purchased += purchased
            total_stock += stock

        categories.sort(key=lambda x: -x["revenue"])
        
        brackets = [("0–25%", 0, 25), ("25–50%", 25, 50), ("50–75%", 50, 75), ("75–100%", 75, 101)]
        n = max(len(categories), 1)
        distribution = []
        for label, lo, hi in brackets:
            count = sum(1 for c in categories if lo <= c["sellThrough"] < hi)
            distribution.append({
                "range": label,
                "count": count,
                "pct": float(f"{100 * count / n:.1f}")
            })

        overall_st = float(f"{100 * total_sold / max(total_purchased, 1):.1f}")
        return {
            "data_source": "powerbi",
            "summary": {
                "totalPurchased": total_purchased, "totalSold": total_sold, "totalStock": total_stock,
                "overallSellThrough": overall_st, "totalRevenue": total_revenue,
                "unsoldStockValue": 0, "categoryCount": len(categories),
                "velocity": float(f"{total_sold / d:.1f}"),
            },
            "categories": categories,
            "distribution": distribution,
        }
    except Exception as e:
        logger.warning(f"Sellthrough DAX failed: {e}")
        return default

def fetch_sellthrough_trend_dax(days: int, region: str = "All Regions") -> Dict[str, Any]:
    """Execute DAX and return sellthrough trend (monthly)."""
    from api.db_powerbi import execute_dax

    try:
        dax_trend = """
EVALUATE
SUMMARIZECOLUMNS(
    'Date_Table'[Month-Year],
    'Date_Table'[MonthSort],
    "sell_through", [Sell Through %],
    "sold", [Qty_Sold],
    "purchased", [Qty_Received]
)
ORDER BY 'Date_Table'[MonthSort] ASC
"""
        rows = execute_dax(dax_trend)
        months = []
        for r in (rows or []):
            rn = _norm_row(r)
            month = str(rn.get("Month-Year") or rn.get("MONTH-YEAR") or "")
            if not month:
                continue
                
            sold = int(rn.get("sold", 0) or 0)
            purchased = int(rn.get("purchased", 0) or 0)
            st_raw = float(rn.get("sell_through", 0) or 0)
            sort_val = rn.get("MonthSort") or 0
            
            if st_raw <= 1.01: 
                st = st_raw * 100
            else:
                st = st_raw
            
            st_fmt = min(float(f"{st:.1f}"), 100.0)
            
            months.append({
                "month": month, 
                "sort_val": sort_val,
                "sold": sold, 
                "purchased": purchased, 
                "sellThroughPct": st_fmt
            })
        
        months.sort(key=lambda x: str(x["sort_val"]))
        return {"months": months}
    except Exception as e:
        logger.warning(f"Trend DAX failed: {e}")
        return {"months": []}
