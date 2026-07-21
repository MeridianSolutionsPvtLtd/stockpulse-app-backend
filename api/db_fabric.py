"""
Fabric SQL Endpoint connection helper.
Revenue Intelligence Space: AZURE_*, WS_REVENUE_ID, MODEL_REVENUE_ID, FABRIC_WORKSPACE_NAME.
"""
import os
import pyodbc
import pandas as pd
from typing import Any

def _env(*keys, default=""):
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default

_ep = _env("FABRIC_SQL_ENDPOINT")
_ws = _env("FABRIC_WORKSPACE_NAME")
FABRIC_SQL_ENDPOINT = _ep or (f"powerbi://api.powerbi.com/v1.0/myorg/{_ws}" if _ws else "")
PBI_CLIENT_ID = _env("AZURE_CLIENT_ID", "PBI_CLIENT_ID", "POWERBI_CLIENT_ID")
PBI_CLIENT_SECRET = _env("AZURE_CLIENT_SECRET", "PBI_CLIENT_SECRET", "POWERBI_CLIENT_SECRET")
PBI_TENANT_ID = _env("AZURE_TENANT_ID", "PBI_TENANT_ID", "POWERBI_TENANT_ID")

def _find_odbc_driver():
    """Try Driver 18 first (Fabric recommended), then 17."""
    installed = set(d or "" for d in (pyodbc.drivers() or []))
    for name in ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]:
        if name in installed:
            return "{" + name + "}"
    raise RuntimeError(
        "No Microsoft ODBC Driver for SQL Server found. Install from: "
        "https://learn.microsoft.com/en-us/sql/connect/odbc/download-odbc-driver-for-sql-server"
    )


def get_connection():
    """Returns a pyodbc connection to the Fabric/Power BI SQL/XMLA Endpoint."""
    if not FABRIC_SQL_ENDPOINT:
        raise ValueError("Set FABRIC_SQL_ENDPOINT or FABRIC_WORKSPACE_NAME (e.g. test1) in environment.")

    driver = _find_odbc_driver()
    
    # Clean up the endpoint URL if it has the powerbi:// prefix
    server = FABRIC_SQL_ENDPOINT
    if server.startswith("powerbi://"):
        # The SQL driver can often handle the powerbi:// prefix, but let's be safe
        pass 

    database = _env("FABRIC_LAKEHOUSE_NAME", "MODEL_REVENUE_ID", "PBI_DATASET_ID", default="StockPulse")
    
    if PBI_CLIENT_ID and PBI_CLIENT_SECRET:
        # Service Principal Auth (Most robust for backend)
        conn_str = (
            f"Driver={driver};"
            f"Server={server};"
            f"Database={database};"
            f"Uid={PBI_CLIENT_ID};"
            f"Pwd={PBI_CLIENT_SECRET};"
            f"Authentication=ActiveDirectoryServicePrincipal;"
        )
    else:
        # User Auth (Interactive)
        conn_str = (
            f"Driver={driver};"
            f"Server={server};"
            f"Database={database};"
            f"Authentication=ActiveDirectoryInteractive;"
        )

    return pyodbc.connect(conn_str)

def query_one(query: str, params=None):
    """Execute SQL and return first row."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params or [])
        row = cursor.fetchone()
        return tuple(row) if row else None

def query_all(query: str, params=None):
    """Execute SQL and return all rows."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params or [])
        rows = cursor.fetchall()
        return [tuple(r) for r in rows]

def query_df(query: str, params=None):
    """Execute SQL and return DataFrame."""
    with get_connection() as conn:
        return pd.read_sql(query, conn, params=params)

def is_configured():
    """Check if basic config is present."""
    return bool(FABRIC_SQL_ENDPOINT or _env("FABRIC_WORKSPACE_NAME"))
