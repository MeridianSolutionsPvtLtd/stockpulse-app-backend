import os
from dotenv import load_dotenv
load_dotenv()

from api import db_fabric
TABLE = "replenishments_recom"
where = ""
db_col = "[Recommended_Refill]"
order = "DESC"
offset = 0
limit = 20

q = f"""
    SELECT
        Site,
        ABS(CAST(Barcode AS BIGINT)) AS Barcode,
        TRIM(Sku) AS Sku,
        SIZE,
        Season,
        Week,
        Sum_of_Sales,
        Sum_of_soh,
        WH_Soh,
        forecasted_refill,
        [Recommended_Refill],
        Recommendation,
        Recommendation_Comment,
        Refill_Note
    FROM {TABLE}
    {where}
    ORDER BY {db_col} {order}
    OFFSET {offset} ROWS FETCH NEXT {limit} ROWS ONLY
"""
print(f"Query:\n{q}")
try:
    rows = db_fabric.query_all(q)
    print(f"Success! Fetched {len(rows)} rows.")
except Exception as e:
    print(f"Failed! Error: {e}")
