from api import db_fabric
TABLE = "replenishments_recom"
rows = db_fabric.query_all(f"SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{TABLE}'")
cols = [r[0] for r in rows]
print(f"Columns: {', '.join(cols)}")
