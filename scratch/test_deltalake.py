import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()
from deltalake import DeltaTable
from api.onelake_store import OneLakeJsonStore

store = OneLakeJsonStore()
uri = store._delta_uri("recom_neww2")
opts = store._delta_storage_options()
print("URI:", uri)
print("Opts:", opts)
try:
    dt = DeltaTable(uri, storage_options=opts)
    print("Table loaded. Version:", dt.version())
except Exception as e:
    print("Failed with current opts:", e)

opts2 = {
    "azure_client_id": opts["client_id"],
    "azure_client_secret": opts["client_secret"],
    "azure_tenant_id": opts["tenant_id"],
}
try:
    dt = DeltaTable(uri, storage_options=opts2)
    print("Table loaded with azure_ prefix. Version:", dt.version())
except Exception as e:
    print("Failed with azure_ prefix:", e)
    
opts3 = {
    "azure_storage_client_id": opts["client_id"],
    "azure_storage_client_secret": opts["client_secret"],
    "azure_storage_tenant_id": opts["tenant_id"],
}
try:
    dt = DeltaTable(uri, storage_options=opts3)
    print("Table loaded with azure_storage_ prefix. Version:", dt.version())
except Exception as e:
    print("Failed with azure_storage_ prefix:", e)
