import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()
from api.db_fabric import query_df
try:
    df = query_df("SELECT TOP 5 * FROM recom_neww2")
    print(df.head())
    print("Columns:", df.columns.tolist())
except Exception as e:
    print("Error:", e)
