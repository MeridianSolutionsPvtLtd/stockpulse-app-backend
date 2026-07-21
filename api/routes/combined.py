from fastapi import APIRouter, HTTPException
from api import db_powerbi
from api import db_fabric
import logging

router = APIRouter(prefix="/api/combined", tags=["Combined Data"])
logger = logging.getLogger(__name__)

@router.get("/summary")
async def get_combined_summary():
    """
    Fetch data from both Power BI (DAX) and Fabric Lakehouse (SQL).
    """
    try:
        # 1. Fetch from Power BI (DAX) - Using a simple query to verify
        # Adjust 'Fact_Sales_Detail' or other table names based on your model
        dax_query = "EVALUATE TOPN(5, SUMMARIZECOLUMNS('Fact_Sales_Detail'[DEPARTMENT], \"Total Sales\", SUM('Fact_Sales_Detail'[Net Sale Amt01/11/2023-26/01/2026])))"
        try:
            pbi_data = db_powerbi.execute_dax(dax_query)
        except Exception as e:
            logger.error(f"Power BI Error: {e}")
            pbi_data = {"error": str(e)}

        # 2. Fetch from Fabric Lakehouse (SQL)
        # We'll try to list tables first to verify connection
        sql_query = "SELECT TOP 5 * FROM INFORMATION_SCHEMA.TABLES"
        try:
            lakehouse_data = db_fabric.query_all(sql_query)
        except Exception as e:
            logger.error(f"Lakehouse Error: {e}")
            lakehouse_data = {"error": str(e)}

        return {
            "source_powerbi": pbi_data,
            "source_lakehouse": lakehouse_data,
            "status": "success"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
