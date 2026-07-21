"""Database connection helper — read-only for API.

Supports two backends:
  - powerbi: Uses DAX queries against Power BI semantic model (Execute Queries API)
  - fabric_sql: Uses SQL queries against Fabric SQL Endpoint

Set DATABASE_BACKEND=fabric_sql in .env.
"""
import os
import sys

from .config import DATABASE_BACKEND

_BACKEND = DATABASE_BACKEND.lower()
_USE_POWERBI = _BACKEND == "powerbi"
_USE_FABRIC = _BACKEND == "fabric_sql"


def get_conn():
    """Return a connection/session."""
    if _USE_POWERBI:
        from .db_powerbi import get_powerbi_session
        return get_powerbi_session()
    if _USE_FABRIC:
        from .db_fabric import get_connection
        return get_connection()
    
    raise RuntimeError(
        "Invalid DATABASE_BACKEND. Set DATABASE_BACKEND=fabric_sql (or powerbi) in .env"
    )


def query_one(conn, query: str, params=None):
    """Execute query and return first row."""
    if _USE_POWERBI:
        from .db_powerbi import query_one_dax
        return query_one_dax(query)
    if _USE_FABRIC:
        from .db_fabric import query_one
        return query_one(query, params)
    raise RuntimeError("Invalid DATABASE_BACKEND. Set DATABASE_BACKEND=fabric_sql in .env")


def query_all(conn, query: str, params=None):
    """Execute query and return all rows."""
    if _USE_POWERBI:
        from .db_powerbi import query_all_dax
        return query_all_dax(query)
    if _USE_FABRIC:
        from .db_fabric import query_all
        return query_all(query, params)
    raise RuntimeError("Invalid DATABASE_BACKEND. Set DATABASE_BACKEND=fabric_sql in .env")


def query_df(conn, query: str, params=None):
    """Execute query and return DataFrame."""
    if _USE_POWERBI:
        from .db_powerbi import query_df_dax
        return query_df_dax(query)
    if _USE_FABRIC:
        from .db_fabric import query_df
        return query_df(query, params)
    raise RuntimeError("Invalid DATABASE_BACKEND. Set DATABASE_BACKEND=fabric_sql in .env")
