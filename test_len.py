from api import db_fabric
import sys

conn = db_fabric.get_connection()
cursor = conn.cursor()
cursor.execute("SELECT TOP 1 * FROM replenishments_recom")
with open("len.txt", "w") as f:
    for desc in cursor.description:
        f.write(f"NAME: '{desc[0]}' LEN: {len(desc[0])}\n")
cursor.close()
conn.close()
