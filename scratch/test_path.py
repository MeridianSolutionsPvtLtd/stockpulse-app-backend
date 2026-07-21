import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()
from api.onelake_store import OneLakeJsonStore

store = OneLakeJsonStore()
paths = store.fs.get_paths(path=f"{store.lakehouse}.Lakehouse/Tables")
for p in paths:
    if "recom_neww2" in p.name:
        print(p.name)
