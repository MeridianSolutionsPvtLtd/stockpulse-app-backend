import os
from dotenv import load_dotenv
from api import db_fabric

load_dotenv()
try:
    df = db_fabric.query_df("SELECT TOP 1 * FROM replenishments_recom")
    print("COLUMNS:", df.columns.tolist())
    print("SAMPLE:", df.iloc[0].to_dict())
except Exception as e:
    print("ERROR:", e)
