from fastapi import APIRouter, Query
import pandas as pd
from typing import List, Optional
from pydantic import BaseModel

router = APIRouter()

class SalesKPIContract(BaseModel):
    units_sold: int
    units_returned: int
    net_units: int
    net_revenue: float
    asp: float
    return_rate: float
    units_30d: int

from api.config import DATABASE_BACKEND

_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"

@router.get("/sales-kpi", response_model=SalesKPIContract)
def get_sales_kpi_contract(region: str = Query("All Regions")):

    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, C_SALE_DATE, C_SALE_AMOUNT, C_SALE_IS_RETURN, C_SALE_QTY, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    zone_filter = ""
    if region != "All Regions":
        zone_filter = f"FILTER(ALL('{T_DS}'), '{T_DS}'[{C_STORE_ZONE}]=\"{region}\")"

    try:
        query = f"""
EVALUATE
VAR MaxDate = MAX('{T_FS}'[{C_SALE_DATE}])
VAR Filtered = {"CALCULATETABLE('{T_FS}', " + zone_filter + ")" if zone_filter else "'{T_FS}'"}
RETURN ROW(
    "units_sold", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), {not_return}, Filtered),
    "units_returned", ABS(CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), '{T_FS}'[{C_SALE_IS_RETURN}] = TRUE(), Filtered)),
    "net_units", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), Filtered),
    "net_revenue", CALCULATE(SUM('{T_FS}'[{C_SALE_AMOUNT}]), Filtered),
    "units_30d", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), '{T_FS}'[{C_SALE_DATE}] >= MaxDate - 30, Filtered)
)
"""
        rows = execute_dax(query)
        r = _norm_row(rows[0]) if rows else {}
        sold = int(r.get("units_sold") or 0)
        ret = int(r.get("units_returned") or 0)
        rev = float(r.get("net_revenue") or 0)
        
        return {
            "units_sold": sold,
            "units_returned": ret,
            "net_units": int(r.get("net_units") or 0),
            "net_revenue": rev,
            "asp": round(rev / max(sold, 1), 2),
            "return_rate": round(100 * ret / max(sold, 1), 2),
            "units_30d": int(r.get("units_30d") or 0)
        }
    except Exception as e:
        import logging; logging.getLogger(__name__).warning("Sales KPI DAX failed: %s", e)
        return { "units_sold": 0, "units_returned": 0, "net_units": 0, "net_revenue": 0.0, "asp": 0.0, "return_rate": 0.0, "units_30d": 0 }


@router.get("/sales-kpi/breakdown/category")
def get_sales_category_breakdown(region: str = Query("All Regions")):
    if not _USE_POWERBI:
        return [] # Fallback omitted for brevity
    
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, T_DP, C_PROD_DEPT, C_SALE_AMOUNT, C_SALE_QTY, C_SALE_IS_RETURN, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    zone_filter = ""
    if region != "All Regions":
        zone_filter = f"FILTER(ALL('{T_DS}'), '{T_DS}'[{C_STORE_ZONE}]=\"{region}\")"

    try:
        query = f"""
EVALUATE
VAR S = SUMMARIZECOLUMNS(
    '{T_DP}'[{C_PROD_DEPT}],
    {zone_filter if zone_filter else ""},
    "units_sold", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), {not_return}),
    "net_revenue", CALCULATE(SUM('{T_FS}'[{C_SALE_AMOUNT}]))
)
RETURN S
ORDER BY [units_sold] DESC
"""
        rows = execute_dax(query)
        total_units = sum(int(r.get("units_sold") or 0) for r in (rows or []))
        items = []
        for r in (rows or []):
            rn = _norm_row(r)
            sold = int(rn.get("units_sold") or 0)
            rev = float(rn.get("net_revenue") or 0)
            items.append({
                "category": rn.get(C_PROD_DEPT) or "Unclassified",
                "units_sold": sold,
                "net_revenue": rev,
                "asp": round(rev / max(sold, 1), 2),
                "sales_share": round(sold / max(total_units, 1), 4)
            })
        return items
    except Exception:
        return []


@router.get("/sales-kpi/breakdown/season")
def get_sales_season_breakdown(region: str = Query("All Regions")):
    if not _USE_POWERBI: return []
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, T_DP, C_SALE_AMOUNT, C_SALE_QTY, C_SALE_IS_RETURN, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    zone_filter = ""
    if region != "All Regions":
        zone_filter = f"FILTER(ALL('{T_DS}'), '{T_DS}'[{C_STORE_ZONE}]=\"{region}\")"

    try:
        query = f"""
EVALUATE
VAR S = SUMMARIZECOLUMNS(
    '{T_DP}'[Season],
    {zone_filter if zone_filter else ""},
    "units_sold", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), {not_return}),
    "net_revenue", CALCULATE(SUM('{T_FS}'[{C_SALE_AMOUNT}]))
)
RETURN S
ORDER BY [units_sold] DESC
"""
        rows = execute_dax(query)
        total_units = sum(int(r.get("units_sold") or 0) for r in (rows or []))
        items = []
        for r in (rows or []):
            rn = _norm_row(r)
            sold = int(rn.get("units_sold") or 0)
            rev = float(rn.get("net_revenue") or 0)
            items.append({
                "season": rn.get("Season") or "Unknown",
                "units_sold": sold,
                "net_revenue": rev,
                "asp": round(rev / max(sold, 1), 2),
                "sales_share": round(sold / max(total_units, 1), 4)
            })
        return items
    except Exception:
        return []


@router.get("/sales-kpi/top-skus")
def get_top_sales_skus(limit: int = 100, region: str = Query("All Regions")):
    if not _USE_POWERBI: return []
    from api.db_powerbi import execute_dax
    from api.schema_config import T_FS, T_DP, C_PROD_DEPT, C_SALE_AMOUNT, C_SALE_QTY, C_SALE_IS_RETURN, T_DS, C_STORE_ZONE
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    not_return = sale_not_return_dax(T_FS, C_SALE_IS_RETURN)
    zone_filter = ""
    if region != "All Regions":
        zone_filter = f"FILTER(ALL('{T_DS}'), '{T_DS}'[{C_STORE_ZONE}]=\"{region}\")"

    try:
        query = f"""
EVALUATE
TOPN({limit},
    SUMMARIZECOLUMNS(
        '{T_DP}'[Barcode],
        '{T_DP}'[SKU],
        '{T_DP}'[Season],
        '{T_DP}'[{C_PROD_DEPT}],
        {zone_filter if zone_filter else ""},
        "units_sold", CALCULATE(SUM('{T_FS}'[{C_SALE_QTY}]), {not_return}),
        "net_revenue", CALCULATE(SUM('{T_FS}'[{C_SALE_AMOUNT}]))
    ),
    [net_revenue], DESC
)
"""
        rows = execute_dax(query)
        items = []
        for r in (rows or []):
            rn = _norm_row(r)
            sold = int(rn.get("units_sold") or 0)
            rev = float(rn.get("net_revenue") or 0)
            items.append({
                "barcode": rn.get("Barcode") or "",
                "sku_code": rn.get("SKU") or "",
                "season": rn.get("Season") or "Unknown",
                "category": rn.get(C_PROD_DEPT) or "Unclassified",
                "units_sold": sold,
                "net_revenue": rev,
                "asp": round(rev / max(sold, 1), 2)
            })
        return items
    except Exception:
        return []


@router.get("/sales-kpi/low-velocity")
def get_low_velocity_skus(limit: int = 100, region: str = Query("All Regions")):
    # Simplified version for now
    return []

