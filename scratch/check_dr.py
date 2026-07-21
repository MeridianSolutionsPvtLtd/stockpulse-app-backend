import os
import sys
from dotenv import load_dotenv

sys.path.append(os.getcwd())
load_dotenv(".env")

from api.onelake_store import OneLakeJsonStore

def main():
    store = OneLakeJsonStore()
    
    for table_name in ["potential_donors", "potential_receivers"]:
        print(f"--- Schema for: {table_name} ---")
        try:
            rows = store.read_delta_table(table_name)
            if rows:
                cols = list(rows[0].keys())
                print(f"Columns: {cols}")
            else:
                print("Table is empty.")
        except Exception as e:
            print(f"Read failed: {e}")

if __name__ == "__main__":
    main()
