import pyodbc
import os
from api import db_fabric

try:
    conn = db_fabric.get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT TOP 1 * FROM replenishments_recom")
    cols = [desc[0] for desc in cursor.description]
    print("Columns:", cols)
    cursor.close()
    conn.close()
except Exception as e:
    print("Error:", str(e))
