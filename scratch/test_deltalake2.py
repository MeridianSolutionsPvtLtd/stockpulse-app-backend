import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()
from deltalake import DeltaTable
from api.onelake_store import OneLakeJsonStore

store = OneLakeJsonStore()
uri = f"abfss://{store.workspace}@onelake.dfs.fabric.microsoft.com/{store.lakehouse}.Lakehouse/Tables/recom_neww2"
opts = store._delta_storage_options()
print("URI:", uri)
try:
    dt = DeltaTable(uri, storage_options=opts)
    print("Table loaded! Version:", dt.version())
    print("Columns:", dt.schema().names)
except Exception as e:
    print("Failed:", e)
