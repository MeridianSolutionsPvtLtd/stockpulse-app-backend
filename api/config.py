"""
API configuration.
"""
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("STOCKPULSE_DATA_DIR") or os.path.join(PROJECT_ROOT, "data")

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(PROJECT_ROOT, '.env'))
except ImportError:
    pass

DATABASE_BACKEND = os.environ.get("DATABASE_BACKEND", "fabric_sql").lower()
