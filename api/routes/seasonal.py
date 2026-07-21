"""Seasonal Planning API — purchases, sales, inventory by season; optional days filter; by-category."""
import logging
from fastapi import APIRouter, Query
from ..db import get_conn, query_all
from ..config import DATABASE_BACKEND

router = APIRouter()
_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"

def _fetch_seasonal_plan_dax(target_season: str, baseline_seasons: list[str], risk_threshold: int, growth_factor: float) -> dict:
    print(f"DEBUG: Entering _fetch_seasonal_plan_dax. Baselines: {baseline_seasons}")
    from api.db_powerbi import execute_dax
    from api.schema_config import (
        T_FS, C_SALE_DATE, C_SALE_IS_RETURN, C_SALE_QTY, C_SALE_BARCODE, C_SALE_SITE, C_SALE_AMOUNT,
        T_FP, C_PURCH_QTY, C_PURCH_BARCODE, C_SALE_DEPT, C_SALE_SECTION, T_DS, C_STORE_ZONE,
        T_DP, C_PROD_BARCODE, C_PROD_DEPT, C_PROD_SECTION
    )
    from api.dax_queries import sale_not_return_dax, _norm_row
    
    # Core variables
    num_seasons = max(len(baseline_seasons), 1)
    seasons_dax = "{" + ", ".join([f'"{s}"' for s in baseline_seasons]) + "}"
    cat_season = f"'{T_DS}'[{C_STORE_ZONE}]"
    cat_dept = C_PROD_DEPT or "DEPARTMENT"
    
    _log = logging.getLogger(__name__)
    _log.info(f"DEBUG: Starting plan for {target_season}. Baselines: {baseline_seasons}")

    try:
        # 1. Fetch SKU Performance
        feature_query = f"""
EVALUATE
FILTER(
    SUMMARIZECOLUMNS(
        'Fact_Sales_Detail'[SECTION],
        'Fact_Sales_Detail'[DEPARTMENT],
        'Fact_Sales_Detail'[Color],
        'Fact_Sales_Detail'[Fabric],
        'Fact_Sales_Detail'[Size],
        FILTER(VALUES({cat_season}), {cat_season} IN {seasons_dax}),
        "mrp", SUM('Fact_Sales_Detail'[MRP]),
        "sold", [Qty_Sold],
        "purch", [Qty_Received],
        "st", [Sell_Through]
    ),
    NOT(ISBLANK([sold]))
)
ORDER BY [sold] DESC
"""
        sku_rows = execute_dax(feature_query) or []

        # 2. Fetch Store performance
        store_query = f"""
EVALUATE
VAR SelectedSeasons = {seasons_dax}
VAR StoreStats = 
    SUMMARIZECOLUMNS(
        'Dim_SKU_Store_Season'[Site_Name],
        FILTER(VALUES({cat_season}), {cat_season} IN SelectedSeasons),
        "ly_sell_through", [Sell_Through],
        "sold", [Qty_Sold],
        "purch", [Qty_Received],
        "risk_skus", CALCULATE(
            DISTINCTCOUNT('Fact_Sales_Detail'[SKU]),
            FILTER(
                ADDCOLUMNS(
                    SUMMARIZE('Fact_Sales_Detail', 'Fact_Sales_Detail'[SKU]),
                    "st", [Sell_Through]
                ),
                [st] < {risk_threshold / 100.0} && NOT(ISBLANK([st]))
            )
        )
    )
RETURN FILTER(StoreStats, NOT(ISBLANK([sold])))
"""
        store_rows = execute_dax(store_query) or []

        # 3. Fetch Top Category per Store
        top_cat_query = f"""
EVALUATE
SUMMARIZECOLUMNS(
    'Dim_SKU_Store_Season'[Site_Name],
    'Fact_Sales_Detail'[DEPARTMENT],
    FILTER(VALUES({cat_season}), {cat_season} IN {seasons_dax}),
    "st", [Sell_Through]
)
"""
        top_cat_rows = execute_dax(top_cat_query) or []
        site_top_cat = {}
        site_max_st = {}
        for r in top_cat_rows:
            rn = _norm_row(r)
            site = str(rn.get("Site_Name") or rn.get("Site") or "Store")
            site_key = "".join(filter(str.isalnum, site.lower()))
            dept = rn.get("DEPARTMENT") or "Unclassified"
            st = float(rn.get("st") or 0)
            if site_key not in site_max_st or st > site_max_st.get(site_key, -1.0):
                site_max_st[site_key] = st
                site_top_cat[site_key] = dept
        
        # 4. Overall season totals
        summary_query = f"""
EVALUATE
VAR SelectedSeasons = {seasons_dax}
VAR AvgST = CALCULATE([Sell_Through], {cat_season} IN SelectedSeasons)
VAR RiskSkus = CALCULATE(
    DISTINCTCOUNT('Fact_Sales_Detail'[SKU]), 
    FILTER(
        ADDCOLUMNS(
            SUMMARIZE('Fact_Sales_Detail', 'Fact_Sales_Detail'[SKU]),
            "st", [Sell_Through]
        ),
        [st] < {risk_threshold / 100.0} && NOT(ISBLANK([st]))
    ),
    {cat_season} IN SelectedSeasons
)
VAR TotalSold = CALCULATE([Qty_Sold], {cat_season} IN SelectedSeasons)
VAR TotalPurch = CALCULATE([Qty_Received], {cat_season} IN SelectedSeasons)
RETURN ROW("st", AvgST, "risk_skus", RiskSkus, "sold", TotalSold, "purch", TotalPurch)
"""
        summ_rows = execute_dax(summary_query) or []
        summ = _norm_row(summ_rows[0]) if summ_rows else {}

        # 5. Categories Allocation
        cat_query = f"""
EVALUATE
FILTER(
    SUMMARIZECOLUMNS(
        'Fact_Sales_Detail'[DEPARTMENT],
        FILTER(VALUES({cat_season}), {cat_season} IN {seasons_dax}),
        "sold", [Qty_Sold],
        "fp", [Full Price Qty],
        "disc", [Discount Qty],
        "purch", [Qty_Received],
        "st", [Sell_Through]
    ),
    NOT(ISBLANK([sold]))
)
ORDER BY [sold] DESC
"""
        cat_rows = execute_dax(cat_query) or []

        def get_hike_multiplier(st_pct: float) -> float:
            if st_pct > 80:
                return 1.2
            elif st_pct >= 50:
                return 1.1
            elif st_pct >= 40:
                return 1.05
            else:
                return 0.9
        
        # 6. Post-process into final format
        skus_list = []
        for r in sku_rows:
            rn = _norm_row(r)
            sold = int(rn.get("sold") or 0)
            st_val = float(rn.get("st") or 0) * 100
            st_pct = min(st_val, 100.0)
            
            hike = get_hike_multiplier(st_pct)
            action = "Increase" if hike > 1 else "Reduce" if hike < 1 else "Maintain"
            
            avg_sold = sold / num_seasons
            rec_units = int(avg_sold * hike)
            
            skus_list.append({
                "section": rn.get("SECTION") or rn.get("Fact_Sales_Detail[SECTION]"),
                "department": rn.get("DEPARTMENT") or rn.get("Fact_Sales_Detail[DEPARTMENT]"),
                "color": rn.get("Color") or rn.get("Fact_Sales_Detail[Color]"),
                "fabric": rn.get("Fabric") or rn.get("Fact_Sales_Detail[Fabric]"),
                "size": rn.get("Size") or rn.get("Fact_Sales_Detail[Size]"),
                "mrp": float(rn.get("mrp") or 0),
                "ly_units": sold,
                "ly_sell_through": round(st_pct, 1),
                "planned": int(avg_sold),
                "recommended": rec_units,
                "delta_pct": round((hike - 1) * 100, 1) if avg_sold > 0 else 0,
                "action": action,
                "risk": "High" if st_pct < risk_threshold else ("Medium" if st_pct < 60 else "Low"),
                "confidence": 85 if st_pct > 60 else 75
            })

        store_list = []
        for r in store_rows:
            rn = _norm_row(r)
            site = str(rn.get("Site_Name") or rn.get("Site") or "Store")
            site_key = "".join(filter(str.isalnum, site.lower()))
            sold = int(rn.get("sold") or 0)
            st_pct = float(rn.get("ly_sell_through") or 0) * 100
            
            hike = get_hike_multiplier(st_pct)
            avg_sold = sold / num_seasons
            s_planned = int(avg_sold)
            s_rec = int(avg_sold * hike)
            
            store_list.append({
                "store": site,
                "planned": s_planned,
                "recommended": s_rec,
                "delta_units": s_rec - s_planned,
                "ly_sell_through": round(st_pct, 1),
                "risk_skus": int(rn.get("risk_skus") or 0),
                "top_category": site_top_cat.get(site_key, "—"),
                "readiness": "Ready" if st_pct > 60 else "In Progress"
            })

        summary_st = float(summ.get("st") or 0) * 100
        summary_hike = get_hike_multiplier(summary_st)
        total_sold = float(summ.get("sold") or 0)
        total_planned = total_sold / num_seasons

        print(f"DEBUG: Summary ST={summary_st}, Hike={summary_hike}, Planned={total_planned}")

        return {
            "summary": {
                "recommendedUnits": int(total_planned * summary_hike),
                "lySellThrough": round(summary_st, 1),
                "deltaPlanPct": round((summary_hike - 1) * 100, 1),
                "totalSoldQty": int(total_sold),
                "avgSoldQty": int(total_sold / num_seasons),
                "atRiskSkus": int(summ.get("risk_skus") or 0)
            },
            "categoryAllocation": [
                {
                    "category": rn.get("DEPARTMENT") or rn.get("Fact_Sales_Detail[DEPARTMENT]"),
                    "current": int(avg_sold),
                    "current_fp": int(fp / num_seasons),
                    "current_disc": int(disc / num_seasons),
                    "recommended": rec_total,
                    "recommended_fp": int((fp / (sold or 1)) * rec_total),
                    "recommended_disc": int((disc / (sold or 1)) * rec_total),
                }
                for rn in [_norm_row(c) for c in cat_rows]
                for sold in [int(rn.get("sold") or 0)]
                for fp in [int(rn.get("fp") or 0)]
                for disc in [int(rn.get("disc") or 0)]
                for avg_sold in [sold / num_seasons]
                for rec_total in [int(avg_sold * get_hike_multiplier(float(rn.get("st") or 0) * 100))]
            ],
            "stores": sorted(store_list, key=lambda x: x['recommended'], reverse=True),
            "skus": skus_list
        }
    except Exception as e:
        _log.error(f"Plan Error: {e}", exc_info=True)
        raise

@router.get("/api/seasonal/plan")
def get_seasonal_plan(
    target_season: str = "SS-26",
    baseline_seasons: list[str] = Query(["SS-24", "SS-25"]),
    growth_factor: float = 1.05,
    risk_threshold: int = 50
):
    if _USE_POWERBI:
        return _fetch_seasonal_plan_dax(target_season, baseline_seasons, risk_threshold, growth_factor)
    return { "summary": { "recommendedUnits": 0, "lySellThrough": 0, "deltaPlanPct": 0, "highRiskSkus": 0 }, "categoryAllocation": [], "stores": [], "skus": [] }
