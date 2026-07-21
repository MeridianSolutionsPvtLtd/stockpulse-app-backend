import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()
from api.onelake_store import OneLakeJsonStore

store = OneLakeJsonStore()
paths = store.fs.get_paths(path=f"{store.lakehouse}.Lakehouse/Files/{store.base_dir}")
for p in paths:
    print(p.name)
