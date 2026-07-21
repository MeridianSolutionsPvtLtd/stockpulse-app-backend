from fastapi import APIRouter, Query, Body, HTTPException
import logging
from typing import List, Optional, Dict, Any
from ..db import get_conn, query_all, query_one
from ..config import DATABASE_BACKEND
import json
import os
import requests
from datetime import datetime
from pydantic import BaseModel

print("[REPLENISHMENT] Module loading...", flush=True)
logger = logging.getLogger(__name__)

router = APIRouter()
_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"
MANUAL_TRANSFERS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "manual_transfers.json")

class ManualTransfer(BaseModel):
    source: str
    destination: str
    sku: str
    quantity: int

def _load_manual_transfers() -> List[Dict[str, Any]]:
    if not os.path.exists(MANUAL_TRANSFERS_FILE):
        return []
    try:
        with open(MANUAL_TRANSFERS_FILE, "r") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []

def _save_manual_transfers(transfers: List[Dict[str, Any]]):
    with open(MANUAL_TRANSFERS_FILE, "w") as f:
        json.dump(transfers, f, indent=2)

def _fetch_replenishment_dax() -> dict:
    from api.db_powerbi import execute_dax
    from api.dax_queries import _norm_row

    logger.info("Executing consolidated DAX query for replenishment...")
    
    q_kpis = """
EVALUATE
ROW(
    "median_dos", [Days_of_Supply],
    "at_risk_units", [SKUs_Not_Sold_180_Days],
    "health_score", [Sell Through %]
)
"""
    q_transfers = """
EVALUATE
ROW(
    "completed_transfers", COUNTROWS(SUMMARIZE('Fact_Stock_Received', 'Fact_Stock_Received'[Date])),
    "units_transferred", SUM('Fact_Stock_Received'[Site Purchase Qty])
)
"""
    q_donors = f"""
EVALUATE
TOPN(
    1500,
    FILTER(
        SUMMARIZECOLUMNS(
            'Fact_Sales_Detail'[SKU],
            'Fact_Sales_Detail'[Site],
            "stock", [Qty_Received] - [Qty_Sold],
            "sales_30d", [Qty_Sold],
            "dos", [Days_of_Supply]
        ),
        [dos] > 90 && NOT(ISBLANK([dos])) && ([Qty_Received] - [Qty_Sold]) > 2
    ),
    [dos], DESC
)
ORDER BY [dos] DESC
"""

    q_receivers = f"""
EVALUATE
TOPN(
    1500,
    FILTER(
        SUMMARIZECOLUMNS(
            'Fact_Sales_Detail'[SKU],
            'Fact_Sales_Detail'[Site],
            "stock", [Qty_Received] - [Qty_Sold],
            "sales_30d", [Qty_Sold],
            "dos", [Days_of_Supply]
        ),
        [dos] >= 1 && [dos] < 14 && NOT(ISBLANK([dos]))
    ),
    [dos], ASC
)
ORDER BY [dos] ASC
"""
    q_mapping = """
EVALUATE
SUMMARIZECOLUMNS(
    'Customer_Master'[SITE NAME],
    'Customer_Master'[SITE MANUAL CODE],
    'Customer_Master'[CITY],
    'Customer_Master'[STATE NAME],
    'Customer_Master'[ZONE],
    "BRAND", MAX('Customer_Master'[CUSTOMER NAME]) 
)
"""
    try:
        from concurrent.futures import ThreadPoolExecutor

        # Define a helper function to safely execute and handle errors per query
        def safe_execute(name, query):
            try:
                return execute_dax(query)
            except Exception as e:
                logger.error(f"{name} failed: {e}")
                return []

        logger.info(f"Queries starting in parallel...")
        
        # Fire all 5 requests simultaneously
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_kpi_ov = executor.submit(safe_execute, "q_kpis", q_kpis)
            future_transfer = executor.submit(safe_execute, "q_transfers", q_transfers)
            future_donors = executor.submit(safe_execute, "q_donors", q_donors)
            future_receivers = executor.submit(safe_execute, "q_receivers", q_receivers)
            future_mapping = executor.submit(safe_execute, "q_mapping", q_mapping)

            kpi_ov_rows = future_kpi_ov.result()
            transfer_kpi_rows = future_transfer.result()
            donor_rows = future_donors.result()
            receiver_rows = future_receivers.result()
            mapping_rows = future_mapping.result()

        main_rows = donor_rows + receiver_rows
        logger.info(f"Parallel queries completed: {len(main_rows)} main rows, {len(mapping_rows)} site rows.")

        units_transferred = 0.0
        completed_transfers = 0
        if transfer_kpi_rows:
            re_kpi = _norm_row(transfer_kpi_rows[0])
            units_transferred = float(re_kpi.get("units_transferred") or 0.0)
            completed_transfers = int(float(re_kpi.get("completed_transfers") or 0.0))

        # KPIs from table 0
        kpis = [
            {"label": "Median DOS", "value": "0", "change": 0, "status": "neutral"},
            {"label": "At-Risk Units", "value": "0", "change": 0, "status": "neutral"},
            {"label": "Stock Health Score", "value": "0%", "change": 0, "status": "neutral"},
            {"label": "Completed Transfers", "value": str(completed_transfers), "change": 0, "status": "neutral"},
            {"label": "Units Transferred", "value": f"{units_transferred/1000.0: .1f}k", "change": 0, "status": "neutral"}
        ]
        if kpi_ov_rows:
            ov = _norm_row(kpi_ov_rows[0])
            m_dos = float(ov.get("median_dos") or 0.0)
            at_risk = float(ov.get('at_risk_units') or 0.0)
            health = float(ov.get('health_score') or 0.0)
            if health <= 1.01: health *= 100
            
            kpis[0]["value"] = f"{m_dos:.1f}"
            kpis[1]["value"] = f"{at_risk/1000.0: .1f}k" if at_risk > 1000 else f"{int(at_risk)}"
            kpis[2]["value"] = f"{min(health, 100.0):.1f}%"

        current_transfers = []
        manuals = _load_manual_transfers()
        for i, mt in enumerate(reversed(manuals)):
            current_transfers.append({
                "id": f"MTRF-{100+i}",
                "source": mt.get("source"),
                "destination": mt.get("destination"),
                "sku": mt.get("sku"),
                "items": mt.get("quantity"),
                "status": "In Transit",
                "eta": mt.get("created_at", "Just Now")
            })

        site_info: Dict[str, Dict[str, Any]] = {}
        site_info_upper: Dict[str, Dict[str, Any]] = {}
        if mapping_rows:
            for r_map in mapping_rows:
                rn = _norm_row(r_map)
                s_id = str(rn.get('SITE') or '')
                s_name = str(rn.get('SITE NAME') or '')
                info: Dict[str, Any] = {
                    'name': s_name,
                    'brand': (str(rn.get('BRAND') or '')).upper().strip(),
                    'city': str(rn.get('CITY') or ''),
                    'state': (str(rn.get('STATE NAME') or '')).upper().strip(),
                    'code': s_id
                }
                if s_id:
                    s_id_u = s_id.upper()
                    site_info[s_id] = info
                    site_info_upper[s_id_u] = info
                if s_name:
                    s_name_u = s_name.upper()
                    site_info[s_name] = info
                    site_info_upper[s_name_u] = info

        donor_stores = []
        receiver_stores = []
        sku_donors: Dict[str, List[Dict[str, Any]]] = {} 
        sku_receivers: Dict[str, List[Dict[str, Any]]] = {} 

        for r_main in main_rows:
            rn = _norm_row(r_main)
            # Case insensitive key lookup
            rn_upper = {str(k).upper(): v for k, v in rn.items()}
            
            site_raw = str(rn_upper.get("SITE") or rn_upper.get("SITE_NAME") or "Store")
            sku_code = str(rn_upper.get("BARCODE") or rn_upper.get("SKU") or "N/A")
            
            stock_val = float(rn_upper.get("STOCK") or 0.0)
            sales_30d = float(rn_upper.get("SALES_30D") or 0.0)
            dos_val = float(rn_upper.get("DOS") or 0.0)
            
            # Recalculate DOS if 0 and possible
            if dos_val <= 0 and sales_30d > 0.1:
                dos_val = stock_val / (sales_30d / 30.0)
            elif dos_val <= 0:
                dos_val = 999.0 if stock_val > 0 else 0.0
            
            info = site_info.get(site_raw) or site_info_upper.get(site_raw.upper())
            if not info:
                u_site = site_raw.upper()
                brand_val = "OTHER"
                if "PANTALOONS" in u_site or u_site.startswith("PT-"): brand_val = "PT"
                elif "SHOPPER STOP" in u_site or "SHOPPERS STOP" in u_site or u_site.startswith("SS-"): brand_val = "SS"
                info = {"brand": brand_val, "city": "", "state": "", "name": site_raw}
            
            c_city = info.get("city", "")
            c_state = info.get("state", "")
            c_brand = info.get("brand", "OTHER")
            c_name = info.get("name", site_raw)
            
            item = {
                "name": c_name, "sku": sku_code, "dos": int(dos_val), "stock": int(stock_val), 
                "city": c_city, "state": c_state, "brand": c_brand
            }
            
            if dos_val > 90.0:
                donor_stores.append(item)
                if sku_code not in sku_donors: sku_donors[sku_code] = []
                give_qty = int(stock_val * 0.3) if stock_val > 10.0 else 0
                if give_qty > 0:
                    sku_donors[sku_code].append({
                        "site": site_raw, "city": c_city, "state": c_state, "brand": c_brand, "give": give_qty, "name": c_name, "dos": dos_val
                    })
            elif 1.0 <= dos_val < 14.0: 
                receiver_stores.append(item)
                if sku_code not in sku_receivers: sku_receivers[sku_code] = []
                sku_receivers[sku_code].append({
                    "site": site_raw, "city": c_city, "state": c_state, "brand": c_brand, "need": 5, "dos": dos_val, "name": c_name
                })

        recommendations = []
        rec_id_ctr = 1
        
        for sku_cd, receivers_list in sku_receivers.items():
            if sku_cd not in sku_donors: continue
            donors_list = sku_donors[sku_cd]
            
            for rec_store in sorted(receivers_list, key=lambda x: float(x['dos'])):
                valid_donors = []
                for d_store in donors_list:
                    if int(d_store['give']) <= 0: continue
                    
                    def is_brand(target_brand, brand_str, site_str):
                        b, s = str(brand_str).upper(), str(site_str).upper()
                        if target_brand == "SS": return "SHOPPER" in b or "SHOPPER" in s
                        if target_brand == "PT": return "PANTALOON" in b or "PANTALOON" in s or s.startswith("PT-") or s.startswith("PT ")
                        return False
                    
                    d_is_pt = is_brand("PT", d_store['brand'], d_store['site'])
                    r_is_pt = is_brand("PT", rec_store['brand'], rec_store['site'])
                    d_is_ss = is_brand("SS", d_store['brand'], d_store['site'])
                    r_is_ss = is_brand("SS", rec_store['brand'], rec_store['site'])

                    # Rule 1: No PT <-> SS mixing under any circumstances
                    if (d_is_pt and r_is_ss) or (d_is_ss and r_is_pt):
                        continue
                    
                    # Rule 2: Strict Brand Isolation for core stores
                    # A Shoppers Stop store can ONLY donate to another SS store, it cannot donate to a franchise/3rd party
                    if d_is_ss and not r_is_ss: continue
                    if r_is_ss and not d_is_ss: continue
                        
                    # A Pantaloons store can ONLY donate to another PT store
                    if d_is_pt and not r_is_pt: continue
                    if r_is_pt and not d_is_pt: continue
                    
                    # Rule 3: PT State Rule
                    if d_is_pt and r_is_pt:
                        if d_store['state'] != rec_store['state']:
                            continue
                    
                    valid_donors.append(d_store)
                
                for vd in valid_donors:
                    if int(vd['give']) <= 0: continue
                    transfer_qty = min(int(vd['give']), int(rec_store['need']))
                    if transfer_qty > 0:
                        import hashlib
                        raw_str = f"{vd['name']}-{rec_store['name']}-{sku_cd}"
                        unique_id = hashlib.md5(raw_str.encode()).hexdigest()[:8]
                        
                        recommendations.append({
                            "id": f"REC-{unique_id}",
                            "source": vd['name'],
                            "destination": rec_store['name'],
                            "sku": sku_cd,
                            "quantity": transfer_qty,
                            "items": transfer_qty,
                            "urgency": "High" if float(rec_store['dos']) < 5.0 else "Medium",
                            "priority": "High" if float(rec_store['dos']) < 5.0 else "Medium",
                            "status": "Recommended",
                            "reason": f"{vd['state'] or 'Unknown'} → {rec_store['state'] or 'Unknown'}",
                            "isLocal": (vd['city'] == rec_store['city'] and vd['city'] != ""),
                            "product": "SKU: " + sku_cd
                        })
                        vd['give'] = int(vd['give']) - transfer_qty
                        rec_id_ctr += 1
                        break

        return {
            "kpi": {
                "completed_month": completed_transfers,
                "units_transferred": int(units_transferred)
            },
            "kpis": kpis, # Keep for backward compat if needed
            "donors": donor_stores[:50],        
            "receivers": receiver_stores[:50],
            "recommendations": recommendations[:100],
            "transfers": recommendations[:100], # Keep for backward compat
            "current_transfers": current_transfers
        }
    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"Error in _fetch_replenishment_dax: {tb}")
        return {
            "kpis": [], "donors": [], "receivers": [], "transfers": [], "current_transfers": [],
            "error": str(exc),
            "traceback": tb,
            "replen_timestamp": str(__import__('datetime').datetime.now())
        }

@router.post("/replenishment/transfer")
def create_manual_transfer(transfer: ManualTransfer):
    """Create a manual transfer."""
    transfers = _load_manual_transfers()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    new_trf = transfer.dict()
    new_trf["created_at"] = now_str
    transfers.append(new_trf)
    _save_manual_transfers(transfers)
    return {"status": "success", "message": "Transfer created."}

@router.get("/replenishment")
def get_replenishment():
    """Main replenishment endpoint."""
    if _USE_POWERBI:
        return _fetch_replenishment_dax()
        
    conn = get_conn()
    try:
        sql = """
            SELECT site_code FROM dim_stores LIMIT 1
        """
        query_all(conn, sql)
        return {"kpis": [], "donors": [], "receivers": [], "transfers": [], "current_transfers": _load_manual_transfers()}
    finally:
        conn.close()
