from api import db_fabric
import json
TABLE = "replenishments_recom"
rows = db_fabric.query_all(f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{TABLE}'")
cols = [r[0] for r in rows]
with open("cols_debug.json", "w") as f:
    json.dump(cols, f)
print("Done")
