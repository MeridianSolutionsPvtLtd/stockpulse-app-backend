# """
# StockPulse Backend API — serves data from Fabric or Power BI semantic model.
# Run: uvicorn main:app --reload --port 5001
# """
# import os
# import sys
# import logging

# # Load .env 
# try:
#     from dotenv import load_dotenv
#     # Look for .env in the same directory as this script (backend/)
#     load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
# except ImportError:
#     pass  # python-dotenv optional

# from fastapi import FastAPI, Request
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.responses import JSONResponse

# from api.routes import dashboard, stores, inventory, analytics, sellthrough, seasonal, alerts, replenishment, targets, transfers, data_insights, ai_planning, sales_kpi, retail_performance

# app = FastAPI(title="StockPulse API", version="1.0.0")
# log = logging.getLogger(__name__)


# _EMPTY_INVENTORY_SUMMARY = {
#     "totalStockUnits": 0, "totalStockValue": 0, "totalUnitsSold": 0, "totalRevenue": 0,
#     "totalUnitsPurchased": 0, "totalSkus": 0, "soldSkus": 0, "noSaleSkus": 0,
#     "overallDaysOfSupply": 0, "dailyVelocity": 0, "sellThrough": 0,
#     "needsReplenishCategories": 0, "overstockUnits": 0, "healthDistribution": {}, "categoryCount": 0,
# }

# _PATH_FALLBACKS = {
#     "/api/dashboard/regions": {"regions": [], "zoneList": ["All Regions"]},
#     "/api/dashboard/summary": {"revenue": 0, "units_sold": 0, "units_purchased": 0, "sell_through_pct": 0, "active_stores": 0, "inventory_units": 0, "avg_discount_pct": 0, "at_risk_skus": 0, "sell_through_trend": 0, "inventory_trend": 0, "discount_trend": 0, "at_risk_trend": 0},
#     "/api/dashboard/region-profile": {"sellThrough": 0, "inventoryValue": 0, "discountRate": 0, "at_risk_skus": 0, "sell_through_trend": 0, "inventory_trend": 0, "discount_trend": 0, "at_risk_trend": 0},
#     "/api/alerts": {"alerts": []},
#     "/api/stores": {"stores": []},
#     "/api/stores/regions": {"regions": ["All Regions"]},
#     "/api/inventory": {"summary": _EMPTY_INVENTORY_SUMMARY, "categories": []},
#     "/api/data-insights/overview": {"stores": 0, "cities": 0, "states": 0, "zones": 0, "uniqueSkus": 0, "barcodes": 0, "purchaseRecords": 0, "salesRecords": 0, "purchaseUnits": 0, "salesUnits": 0, "revenue": 0, "avgDiscount": 0},
#     "/api/data-insights/quality-summary": {"returns": {"count": 0, "percentage": 0}, "negativeDiscount": {"count": 0, "percentage": 0}, "extremeDiscount": {"count": 0, "percentage": 0}, "validData": {"count": 0, "percentage": 0}, "missingZones": 0, "missingGrades": 0, "totalRecords": 0},
#     "/api/data-insights/zone-distribution": {"zones": []},
#     "/api/data-insights/season-distribution": {"seasons": []},
#     "/api/data-insights/inventory-movement": {"totalSkus": 0, "soldSkus": 0, "neverSoldSkus": 0, "neverSoldPercentage": 0},
#     "/api/data-insights/idle-time": {"distribution": [], "summary": {"slowMoving": 0, "slowMovingPct": 0, "deadStock": 0, "deadStockPct": 0, "staleStock": 0, "staleStockPct": 0}},
# }


# @app.exception_handler(Exception)
# def handle_db_errors(request: Request, exc: Exception):
#     """Return 200 with empty structure when DB/query fails (AKS empty DB)."""
#     path = request.url.path.split("?")[0]
#     fallback = _PATH_FALLBACKS.get(path)
#     if fallback is None:
#         if path == "/api/stores/regions":
#             fallback = {"regions": ["All Regions"]}
#         elif path.startswith("/api/stores/"):
#             fallback = {"stores": []} if path.count("/") == 3 else {"siteCode": "", "categories": []}
#         elif path.startswith("/api/inventory/"):
#             fallback = {"category": "", "skus": [], "totalSkus": 0}
#     if fallback is None:
#         fallback = {}
#     log.warning("API fallback for %s: %s", path, exc)
#     return JSONResponse(status_code=200, content=fallback)

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# app.include_router(dashboard.router, prefix="/api", tags=["dashboard"])
# app.include_router(stores.router, prefix="/api", tags=["stores"])
# app.include_router(inventory.router, prefix="/api", tags=["inventory"])
# app.include_router(analytics.router, prefix="/api", tags=["analytics"])
# app.include_router(sellthrough.router, prefix="/api", tags=["sellthrough"])
# app.include_router(seasonal.router, prefix="/api", tags=["seasonal"])
# app.include_router(alerts.router, prefix="/api", tags=["alerts"])
# app.include_router(replenishment.router, prefix="/api", tags=["replenishment"])
# app.include_router(targets.router, prefix="/api", tags=["targets"])
# app.include_router(transfers.router, prefix="/api", tags=["transfers"])
# app.include_router(data_insights.router, prefix="/api", tags=["data-insights"])
# app.include_router(ai_planning.router, prefix="/api/ai", tags=["ai-planning"])
# app.include_router(sales_kpi.router, prefix="/api", tags=["sales-kpi"])
# app.include_router(retail_performance.router, prefix="/api", tags=["retail-performance"])



# @app.get("/api/health")
# def health():
#     return {"status": "ok"}


# @app.get("/api/data-source")
# def data_source():
#     """Returns the configured data source for verification."""
#     from api.config import DATABASE_BACKEND
#     backend = DATABASE_BACKEND.lower()
#     return {
#         "data_source": "fabric_sql" if backend == "fabric_sql" else ("powerbi" if backend == "powerbi" else "unknown"),
#         "database_backend": backend,
#         "fabric_workspace": os.environ.get("FABRIC_WORKSPACE_NAME") or os.environ.get("FABRIC_SQL_ENDPOINT", ""),
#         "model_id": os.environ.get("MODEL_REVENUE_ID") or os.environ.get("PBI_DATASET_ID", ""),
#     }


# @app.get("/api/debug/sellthrough-tables")
# def debug_sellthrough_tables():
#     """Probe Fact_Sales_Agg and Fact_Stock_Received to discover real column names."""
#     import traceback
#     from api.db_powerbi import execute_dax
#     result = {}
#     for tbl in ["Fact_Sales_Agg", "Fact_Stock_Received"]:
#         try:
#             rows = execute_dax(f"EVALUATE ROW(\"cnt\", COUNTROWS('{tbl}'))")
#             cnt = rows[0].get("[cnt]") if rows else "?"
#             result[tbl] = {"row_count": cnt, "error": None}
#             # Try to get first row to inspect columns
#             try:
#                 sample = execute_dax(f"EVALUATE TOPN(1, '{tbl}')")
#                 result[tbl]["sample_columns"] = list(sample[0].keys()) if sample else []
#             except Exception as e2:
#                 result[tbl]["sample_columns"] = f"Error: {e2}"
#         except Exception as e:
#             result[tbl] = {"row_count": None, "error": str(e)}
#     return result


# @app.get("/api/debug/discount-by-dept")
# def debug_discount_by_dept():
#     """Probe Fact_Sales_Detail columns and test [1Discount %] measure."""
#     from api.db_powerbi import execute_dax
#     results = {}
#     # Step 1: probe Fact_Sales_Detail columns
#     try:
#         sample = execute_dax("EVALUATE TOPN(1, 'Fact_Sales_Detail')")
#         results["fact_sales_detail_columns"] = list(sample[0].keys()) if sample else []
#     except Exception as e:
#         results["fact_sales_detail_columns"] = f"ERROR: {e}"
#     # Step 2: try the full SUMMARIZECOLUMNS with [1Discount %]
#     dax = """
# EVALUATE
# SUMMARIZECOLUMNS(
#     'Fact_Sales_Detail'[Department],
#     "discount_pct", CALCULATE([1Discount %])
# )
# """
#     try:
#         rows = execute_dax(dax)
#         results["discount_by_dept"] = rows
#     except Exception as e:
#         results["discount_by_dept_error"] = str(e)
#     return results

#     return results


# @app.get("/api/debug/stock-by-site")
# def debug_stock_by_site():
#     """Test Dual Query logic"""
#     from api.db_powerbi import execute_dax
#     q1 = "EVALUATE SUMMARIZECOLUMNS('Fact_Stock_Received'[Site], \"qty_received\", [Qty_Received])"
#     q2 = "EVALUATE SUMMARIZECOLUMNS('Fact_Sales_Detail'[Site], \"qty_sold\", [Qty_Sold])"
    
#     try:
#         r1 = execute_dax(q1)
#         r2 = execute_dax(q2)
        
#         # Join in python
#         sites = {}
#         for r in r1:
#             site = r.get("Fact_Stock_Received[Site]")
#             if site:
#                 sites[site] = {"qr": r.get("[qty_received]", 0), "qs": 0}
                
#         for r in r2:
#             site = r.get("Fact_Sales_Detail[Site]")
#             if site and site in sites:
#                 sites[site]["qs"] = r.get("[qty_sold]", 0)
                
#         results = []
#         for s, v in sites.items():
#             st = v["qr"] - v["qs"]
#             if 0 <= st < 10:
#                 results.append({"site": s, "stock": st, "qr": v["qr"], "qs": v["qs"]})
                
#         results.sort(key=lambda x: x["stock"])
#         return results[:5]
#     except Exception as e:
#         return {"error": str(e)}


# @app.get("/api/debug/at-risk-test")
# def debug_at_risk():
#     """Directly test the at-risk SKU DAX and return raw result or error."""
#     from api.db_powerbi import execute_dax
#     dax_query = """
# EVALUATE
# VAR MaxDate = CALCULATE(MAX('Fact_Sales_Agg'[Date]))
# VAR SkusIn180Days = CALCULATETABLE(
#     VALUES('Fact_Stock_Received'[SKU]),
#     'Fact_Stock_Received'[Date] >= MaxDate - 179,
#     'Fact_Stock_Received'[Date] <= MaxDate
# )
# VAR Universe = COUNTROWS(SkusIn180Days)
# VAR SkusWithRecentSales = CALCULATE(
#     DISTINCTCOUNT('Fact_Sales_Agg'[SKU]),
#     TREATAS(SkusIn180Days, 'Fact_Sales_Agg'[SKU]),
#     'Fact_Sales_Agg'[Date] >= MaxDate - 13,
#     'Fact_Sales_Agg'[Date] <= MaxDate,
#     'Fact_Sales_Agg'[Sales_Qty] > 0
# )
# VAR AtRisk = Universe - IF(ISBLANK(SkusWithRecentSales), 0, SkusWithRecentSales)
# RETURN ROW(
#     "max_date", MaxDate,
#     "universe_180d", Universe,
#     "sold_last_14d", SkusWithRecentSales,
#     "at_risk", AtRisk
# )
# """
#     try:
#         rows = execute_dax(dax_query)
#         return {"success": True, "rows": rows}
#     except Exception as e:
#         return {"success": False, "error": str(e)}



# @app.get("/api/debug/dax-test")
# def debug_dax_test():
#     """Run progressive DAX tests to pinpoint schema/DAX failures. Call this to see the real error."""
#     import traceback
#     from api.schema_config import T_FS, C_SALE_DATE, C_SALE_AMOUNT, C_SALE_QTY
#     result = {"steps": [], "ok": False, "error": None}
#     execute_dax = None
#     try:
#         from api.db_powerbi import execute_dax

#         # Step 1: Simplest possible DAX (no table reference)
#         try:
#             rows = execute_dax('EVALUATE ROW("n", 1)')
#             result["steps"].append({"step": 1, "name": "EVALUATE ROW (no tables)", "ok": True, "rows": len(rows or [])})
#         except Exception as e:
#             result["steps"].append({"step": 1, "name": "EVALUATE ROW (no tables)", "ok": False, "error": str(e)})
#             result["error"] = f"Step 1 failed - API or permissions issue: {e}"
#             return result

#         # Step 2: Table exists (COUNTROWS)
#         try:
#             rows = execute_dax(f'EVALUATE ROW("cnt", COUNTROWS({T_FS}))')
#             result["steps"].append({"step": 2, "name": f"COUNTROWS({T_FS})", "ok": True, "rows": rows})
#         except Exception as e:
#             result["steps"].append({"step": 2, "name": f"COUNTROWS({T_FS})", "ok": False, "error": str(e)})
#             result["error"] = f"Step 2 failed - table '{T_FS}' may not exist: {e}"
#             return result

#         # Step 3: Date column exists
#         try:
#             rows = execute_dax(f'EVALUATE ROW("maxdate", MAX({T_FS}[{C_SALE_DATE}]))')
#             result["steps"].append({"step": 3, "name": f"MAX({T_FS}[{C_SALE_DATE}])", "ok": True, "rows": rows})
#         except Exception as e:
#             result["steps"].append({"step": 3, "name": f"MAX([{C_SALE_DATE}])", "ok": False, "error": str(e)})
#             result["error"] = f"Step 3 failed - column '{C_SALE_DATE}' may not exist: {e}"
#             return result

#         # Step 4: Amount/Qty columns (often the cause of 400 if they're measures, not columns)
#         for col, label in [(C_SALE_AMOUNT, "amount"), (C_SALE_QTY, "qty")]:
#             try:
#                 rows = execute_dax(f'EVALUATE ROW("sum_{label}", SUM({T_FS}[{col}]))')
#                 result["steps"].append({"step": f"4_{label}", "name": f"SUM([{col}])", "ok": True, "rows": rows})
#             except Exception as e:
#                 result["steps"].append({"step": f"4_{label}", "name": f"SUM([{col}])", "ok": False, "error": str(e)})
#                 result["error"] = f"Column '{col}' may not exist or is a measure (use a base column): {e}"

#         result["ok"] = True
#         return result
#     except Exception as e:
#         result["error"] = str(e)
#         result["trace"] = traceback.format_exc()
#         log.exception("dax-test failed")
#         return result


# @app.get("/api/debug/schema")
# def debug_schema():
#     """Return current semantic model table/column mapping (from .env or defaults)."""
#     from api import schema_config
#     return {
#         "tables": {
#             "sales": schema_config.T_FS,
#             "purchases": schema_config.T_FP,
#             "inventory": schema_config.T_FI,
#             "stores": schema_config.T_DS,
#             "products": schema_config.T_DP,
#         },
#         "columns": {
#             "sales": {"date": schema_config.C_SALE_DATE, "amount": schema_config.C_SALE_AMOUNT, "qty": schema_config.C_SALE_QTY, "is_return": schema_config.C_SALE_IS_RETURN, "site": schema_config.C_SALE_SITE, "barcode": schema_config.C_SALE_BARCODE, "discount": schema_config.C_SALE_DISCOUNT},
#             "purchases": {"date": schema_config.C_PURCH_DATE, "qty": schema_config.C_PURCH_QTY, "site": schema_config.C_PURCH_SITE, "barcode": schema_config.C_PURCH_BARCODE},
#             "inventory": {"date": schema_config.C_INV_DATE, "qty": schema_config.C_INV_QTY, "barcode": schema_config.C_INV_BARCODE, "site": schema_config.C_INV_SITE},
#             "stores": {"site": schema_config.C_STORE_SITE, "zone": schema_config.C_STORE_ZONE},
#             "products": {"barcode": schema_config.C_PROD_BARCODE, "department": schema_config.C_PROD_DEPT, "section": schema_config.C_PROD_SECTION},
#         },
#         "env_vars": "Set PBI_TABLE_* and PBI_COL_* in .env to override (see api/schema_config.py)",
#     }


# @app.get("/api/debug/db-status")
# def debug_db_status():
#     """Diagnostic endpoint: test Fabric/DB connection and surface any error."""
#     import traceback
#     from api.config import DATABASE_BACKEND
#     backend = DATABASE_BACKEND.lower()
#     result = {"backend": backend, "ok": False, "error": None, "detail": None}
#     try:
#         if backend == "powerbi":
#             from api.db_powerbi import execute_dax
#             rows = execute_dax("EVALUATE ROW(\"n\", 1)")
#             result["ok"] = bool(rows and len(rows) > 0)
#             result["detail"] = "Power BI Execute Queries API succeeded" if result["ok"] else "No rows returned"
#         else:
#             from api.db import get_conn, query_one
#             conn = get_conn()
#             r = query_one(conn, "SELECT 1 AS n") if backend == "fabric_sql" else query_one(conn, "SELECT 1 AS n")
#             result["ok"] = r is not None
#             result["detail"] = "Connection and basic query succeeded" if r else "Query returned no rows"
#             if hasattr(conn, "close"):
#                 conn.close()
#     except Exception as e:
#         result["error"] = str(e)
#         result["detail"] = traceback.format_exc()
#         log.exception("db-status diagnostic failed")
#     return result
 
 
"""
StockPulse Backend API — serves data from Fabric or Power BI semantic model.
Run: uvicorn main:app --reload --port 5001
"""
 
import os
import logging
from pathlib import Path

print("BACKEND RELOADED AT: " + str(__import__('datetime').datetime.now()))
# Load .env
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
 
from api.routes import (
    dashboard,
    stores,
    inventory,
    analytics,
    sellthrough,
    seasonal,
    alerts,
    replenishment,
    targets,
    transfers,
    data_insights,
    ai_planning,
    sales_kpi,
    retail_performance,
    combined,
    ai_seasonal,
    replenishments_recom,
    transfers_recom,
    download_logs,
)
 
app = FastAPI(title="StockPulse API", version="1.0.0")
print("[RELOAD TRIGGER] Dashboard All should be live.", flush=True)
print("[RELOAD TRIGGER 3] Forcing reload for 0_sales_soh_new fix.", flush=True)
print("[RELOAD TRIGGER 2] Final cleanup applied.", flush=True)
log = logging.getLogger(__name__)

print("\n" + "#"*40)
print("### BACKEND IS LIVE AND RELOADING! ###")
print("#"*40 + "\n", flush=True)
logging.critical("CRITICAL: BACKEND STARTUP IN PROGRESS")
 
# ---------- FRONTEND BUILD PATH ----------
BASE_DIR = Path(__file__).resolve().parent
DIST_DIR = BASE_DIR / "dist"
 
# Mount Vite assets
if DIST_DIR.exists():
    assets_path = DIST_DIR / "assets"
    if assets_path.exists():
        app.mount("/assets", StaticFiles(directory=assets_path), name="assets")
 
# ---------- FALLBACK STRUCTURES ----------
_EMPTY_INVENTORY_SUMMARY = {
    "totalStockUnits": 0,
    "totalStockValue": 0,
    "totalUnitsSold": 0,
    "totalRevenue": 0,
    "totalUnitsPurchased": 0,
    "totalSkus": 0,
    "soldSkus": 0,
    "noSaleSkus": 0,
    "overallDaysOfSupply": 0,
    "dailyVelocity": 0,
    "sellThrough": 0,
    "needsReplenishCategories": 0,
    "overstockUnits": 0,
    "healthDistribution": {},
    "categoryCount": 0,
}
 
 
_PATH_FALLBACKS = {
    "/api/dashboard/regions": {"regions": [], "zoneList": ["All Regions"]},
    "/api/dashboard/summary": {"revenue": 0, "units_sold": 0, "units_purchased": 0, "sell_through_pct": 0, "active_stores": 0, "inventory_units": 0, "avg_discount_pct": 0, "at_risk_skus": 0, "sell_through_trend": 0, "inventory_trend": 0, "discount_trend": 0, "at_risk_trend": 0},
    "/api/dashboard/region-profile": {"sellThrough": 0, "inventoryValue": 0, "discountRate": 0, "at_risk_skus": 0, "sell_through_trend": 0, "inventory_trend": 0, "discount_trend": 0, "at_risk_trend": 0},
    "/api/alerts": {"alerts": []},
    "/api/stores": {"stores": []},
    "/api/stores/regions": {"regions": ["All Regions"]},
    "/api/inventory": {"summary": _EMPTY_INVENTORY_SUMMARY, "categories": []},
    "/api/data-insights/overview": {"stores": 0, "cities": 0, "states": 0, "zones": 0, "uniqueSkus": 0, "barcodes": 0, "purchaseRecords": 0, "salesRecords": 0, "purchaseUnits": 0, "salesUnits": 0, "revenue": 0, "avgDiscount": 0},
    "/api/data-insights/quality-summary": {"returns": {"count": 0, "percentage": 0}, "negativeDiscount": {"count": 0, "percentage": 0}, "extremeDiscount": {"count": 0, "percentage": 0}, "validData": {"count": 0, "percentage": 0}, "missingZones": 0, "missingGrades": 0, "totalRecords": 0},
    "/api/data-insights/zone-distribution": {"zones": []},
    "/api/data-insights/season-distribution": {"seasons": []},
    "/api/data-insights/inventory-movement": {"totalSkus": 0, "soldSkus": 0, "neverSoldSkus": 0, "neverSoldPercentage": 0},
    "/api/data-insights/idle-time": {"distribution": [], "summary": {"slowMoving": 0, "slowMovingPct": 0, "deadStock": 0, "deadStockPct": 0, "staleStock": 0, "staleStockPct": 0}},
}
 
# ---------- LOGGING MIDDLEWARE ----------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    print(f"\n>>> RECEIVED REQUEST: {request.method} {request.url.path}", flush=True)
    logger.info(f"RECEIVED REQUEST: {request.method} {request.url.path}")
    response = await call_next(request)
    print(f"<<< COMPLETED REQUEST: {request.method} {request.url.path} with status {response.status_code}\n", flush=True)
    logger.info(f"COMPLETED REQUEST: {request.method} {request.url.path} with status {response.status_code}")
    return response

# ---------- ERROR HANDLER ----------
@app.exception_handler(Exception)
def handle_db_errors(request: Request, exc: Exception):
    path = request.url.path.split("?")[0]
    fallback = _PATH_FALLBACKS.get(path, {})
    
    # Capture error message to return to frontend
    err_msg = str(exc)
    is_capacity_error = "capacity has exceeded its limits" in err_msg.lower()
    
    log.warning("API error for %s: %s", path, exc)
    
    # If it's a capacity error, we can include it in the response so the frontend knows
    if isinstance(fallback, dict):
        fallback["error"] = err_msg
        fallback["is_capacity_error"] = is_capacity_error
        
    return JSONResponse(status_code=200, content=fallback)
 
# ---------- CORS ----------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
 
# ---------- ROUTERS ----------
app.include_router(dashboard.router, prefix="/api", tags=["dashboard"])
app.include_router(stores.router, prefix="/api", tags=["stores"])
app.include_router(inventory.router, prefix="/api", tags=["inventory"])
app.include_router(analytics.router, prefix="/api", tags=["analytics"])
app.include_router(sellthrough.router, prefix="/api", tags=["sellthrough"])
app.include_router(seasonal.router, tags=["seasonal"])
app.include_router(alerts.router, prefix="/api", tags=["alerts"])
app.include_router(replenishment.router, prefix="/api", tags=["replenishment"])
app.include_router(targets.router, prefix="/api", tags=["targets"])
app.include_router(transfers.router, prefix="/api", tags=["transfers"])
app.include_router(data_insights.router, prefix="/api", tags=["data-insights"])
app.include_router(ai_planning.router, prefix="/api/ai", tags=["ai-planning"])
app.include_router(sales_kpi.router, prefix="/api", tags=["sales-kpi"])
app.include_router(retail_performance.router, prefix="/api", tags=["retail-performance"])
app.include_router(combined.router, tags=["combined"])
app.include_router(ai_seasonal.router, tags=["ai-seasonal"])
app.include_router(replenishments_recom.router, tags=["replenishments-recom"])
app.include_router(transfers_recom.router, tags=["transfers-recom"])
app.include_router(download_logs.router)
 
# ---------- HEALTH ----------
@app.get("/api/health")
def health():
    print("[DIAGNOSTIC] /api/health was hit!", flush=True)
    return {"status": "ok", "backend": "live", "reload_time": str(__import__('datetime').datetime.now())}

@app.get("/api/ping")
def ping():
    print("[DIAGNOSTIC] /api/ping was hit!", flush=True)
    return "pong"

@app.get("/api/debug/db-status")
def debug_db_status():
    """Diagnostic endpoint to check Power BI / Fabric connection and surface errors."""
    import traceback
    from api.config import DATABASE_BACKEND
    backend = DATABASE_BACKEND.lower()
    result = {"backend": backend, "ok": False, "error": None, "detail": None}
    try:
        if backend == "powerbi":
            from api.db_powerbi import execute_dax
            # Try a simple DAX query that doesn't depend on tables
            rows = execute_dax("EVALUATE ROW(\"test\", 1)")
            result["ok"] = bool(rows and len(rows) > 0)
            result["detail"] = "Power BI Execute Queries API succeeded" if result["ok"] else "No rows returned"
        else:
            from api.db import get_conn, query_one
            conn = get_conn()
            r = query_one(conn, "SELECT 1 AS n")
            result["ok"] = r is not None
            result["detail"] = "SQL connection succeeded"
            if hasattr(conn, "close"):
                conn.close()
    except Exception as e:
        result["error"] = str(e)
        result["detail"] = traceback.format_exc()
        logger.exception("DB Status check failed")
    return result
 
# ---------- FRONTEND ----------
@app.get("/")
def serve_react():
    """Serve React frontend"""
    if DIST_DIR.exists():
        return FileResponse(DIST_DIR / "index.html")
    return {"message": "Frontend build not found"}
 
# ---------- SPA ROUTING ----------
@app.get("/{full_path:path}")
def serve_spa(full_path: str):
    """React Router fallback"""
    file_path = DIST_DIR / full_path
 
    if file_path.exists():
        return FileResponse(file_path)
 
    fallback_index = DIST_DIR / "index.html"
    if fallback_index.exists():
        return FileResponse(fallback_index)
        
    return {"status": "error", "message": f"Path '{full_path}' not found and frontend build is missing."}
 