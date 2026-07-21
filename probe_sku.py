
import pyodbc
import os

# Trying to find the connection string from db_fabric
# I'll just try to import it and use its query_all or session

from api import db_fabric

try:
    q = """
        SELECT TOP 5
            COALESCE(Sku, SSL_Clr)    AS sku,
            SUM(CAST(Sum_of_Sales AS BIGINT))         AS total_sales,
            SUM(Recommended_Refill)   AS total_refill,
            SUM(WH_Soh)               AS total_wh_soh
        FROM replenishments_recom
        GROUP BY Sku, SSL_Clr
        ORDER BY total_sales DESC
    """
    rows = db_fabric.query_all(q)
    print("Success:", rows)
except Exception as e:
    print("Error:", str(e))
