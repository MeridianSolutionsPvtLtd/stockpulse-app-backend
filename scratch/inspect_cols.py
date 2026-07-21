import os
import sys
from dotenv import load_dotenv

# Add current directory to path to import api
sys.path.append(os.getcwd())
load_dotenv(".env")

from api.onelake_store import OneLakeJsonStore

def main():
    store = OneLakeJsonStore()
    table_name = "recommended_transfers"
    
    print(f"Checking table: {table_name}")
    
    # Try reading as Delta table first
    try:
        from deltalake import DeltaTable
        uri = store._delta_uri(table_name)
        dt = DeltaTable(uri, storage_options=store._delta_storage_options())
        cols = dt.to_pyarrow_table().column_names
        print(f"Delta Table Found! Columns ({len(cols)}):")
        for c in cols:
            print(f" - {c}")
        return
    except Exception as e:
        print(f"Delta Table read failed: {e}")

    # Try reading as JSON
    try:
        rows = store.read_table(table_name)
        if rows:
            cols = list(rows[0].keys())
            print(f"JSON File Found! Columns ({len(cols)}):")
            for c in cols:
                print(f" - {c}")
        else:
            print("JSON File is empty or not found.")
    except Exception as e:
        print(f"JSON read failed: {e}")

if __name__ == "__main__":
    main()
