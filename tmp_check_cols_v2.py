import os
from dotenv import load_dotenv
load_dotenv()
from api import db_fabric
import pandas as pd

try:
    df = db_fabric.query_df("SELECT TOP 1 * FROM replenishments_recom")
    print("COLUMNS:", df.columns.tolist())
    # print sample row as dict
    if not df.empty:
        print("SAMPLE:", df.iloc[0].to_dict())
    else:
        print("TABLE IS EMPTY")
except Exception as e:
    print("ERROR:", str(e))
