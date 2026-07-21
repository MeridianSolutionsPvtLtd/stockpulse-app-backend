from api import db_fabric
import sys

try:
    cols = ["Recommended_Refill", "[Recommended_Refill]", "Recommended_refill", "[Recommended_refill]"]
    for c in cols:
        try:
            db_fabric.query_all(f"SELECT TOP 1 {c} FROM replenishments_recom")
            print(f"OK: {c}")
        except Exception as e:
            print(f"FAIL: {c} -> {e}")
except Exception as e:
    print(f"GLOBAL FAIL: {e}")
