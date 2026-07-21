from fastapi import APIRouter, Query
import logging
from api.db import get_conn, query_all
from api.config import DATABASE_BACKEND

logger = logging.getLogger(__name__)

router = APIRouter()

_USE_POWERBI = (DATABASE_BACKEND or "").lower() == "powerbi"

@router.get("/sellthrough")
def get_sellthrough_overview(region: str = Query("All Regions"), days: int = Query(30)):
    """Category-level sell-through with summary KPIs and distribution."""
    logger.info(f"Sell-through overview request: region={region}, days={days}")
    if _USE_POWERBI:
        try:
            from api.dax_queries import fetch_sellthrough_dax
            return fetch_sellthrough_dax(days or 30, region)
        except Exception as e:
            logger.exception(f"Sellthrough DAX failed: {e}")
            return {
                "data_source": "powerbi",
                "summary": {"totalPurchased": 0, "totalSold": 0, "totalStock": 0, "overallSellThrough": 0, "totalRevenue": 0, "unsoldStockValue": 0, "categoryCount": 0, "velocity": 0},
                "categories": [],
                "distribution": [{"range": "0–25%", "count": 0, "pct": 0}, {"range": "25–50%", "count": 0, "pct": 0}, {"range": "50–75%", "count": 0, "pct": 0}, {"range": "75–100%", "count": 0, "pct": 0}],
                "_error": str(e),
            }

    conn = get_conn()
    try:
        d = int(days) if days and days > 0 else 30
        # SQL logic here...
        return {"categories": []} # Simplified for now
    except Exception as e:
        logger.exception(f"Sellthrough SQL failed: {e}")
        return {"categories": []}
    finally:
        conn.close()

@router.get("/sellthrough/trend")
def get_sellthrough_trend(region: str = Query("All Regions"), days: int = Query(30)):
    """Monthly trend of sell-through."""
    logger.info(f"Sell-through trend request: region={region}, days={days}")
    if _USE_POWERBI:
        try:
            from api.dax_queries import fetch_sellthrough_trend_dax
            return fetch_sellthrough_trend_dax(days or 30, region)
        except Exception as e:
            logger.exception(f"Sellthrough trend DAX failed: {e}")
            return {"months": [], "_error": str(e)}
            
    return {"months": []}
