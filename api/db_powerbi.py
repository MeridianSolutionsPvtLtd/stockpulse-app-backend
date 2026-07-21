"""
Power BI semantic model connection via Execute Queries REST API.

Supports:
  1. Service Principal (Client Credentials)
  2. Interactive User Login (via MSAL PublicClientApplication)

Requires (Service Principal):
  - POWERBI_TENANT_ID, POWERBI_CLIENT_ID, POWERBI_CLIENT_SECRET
Requires (Interactive):
  - POWERBI_TENANT_ID, POWERBI_CLIENT_ID
  - POWERBI_AUTH_MODE=interactive

Note: Power BI semantic models use DAX, not SQL. All queries must be DAX.
"""
import os
import time
from typing import Any

import requests
import pandas as pd
import msal
import logging

# Set up logging
logger = logging.getLogger(__name__)
# logging.basicConfig(level=logging.INFO) # This is handled in main.py

# ---------------------------------------------------------------------------
# Configuration (PBI_*, AZURE_*, WS_REVENUE_ID, MODEL_REVENUE_ID supported)
# ---------------------------------------------------------------------------
def _env(*keys, default=""):
    for k in keys:
        v = os.environ.get(k)
        if v:
            return v
    return default

# Revenue Intelligence Space: AZURE_*, WS_REVENUE_ID, MODEL_REVENUE_ID (primary)
POWERBI_TENANT_ID = _env("AZURE_TENANT_ID", "PBI_TENANT_ID", "POWERBI_TENANT_ID")
POWERBI_CLIENT_ID = _env("AZURE_CLIENT_ID", "PBI_CLIENT_ID", "POWERBI_CLIENT_ID")
POWERBI_CLIENT_SECRET = _env("AZURE_CLIENT_SECRET", "PBI_CLIENT_SECRET", "POWERBI_CLIENT_SECRET")
POWERBI_GROUP_ID = _env("WS_REVENUE_ID", "PBI_GROUP_ID", "POWERBI_GROUP_ID")  # Empty = My workspace
POWERBI_DATASET_ID = _env("MODEL_REVENUE_ID", "PBI_DATASET_ID", "POWERBI_DATASET_ID")

# Auth Mode: 'service_principal' (default) or 'interactive'
POWERBI_AUTH_MODE = os.environ.get("POWERBI_AUTH_MODE", "service_principal").lower()

TOKEN_URL = f"https://login.microsoftonline.com/{POWERBI_TENANT_ID}/oauth2/v2.0/token"
# Use analysis.windows.net scope (api.powerbi.com not found in some tenants)
POWERBI_SCOPE = ["https://analysis.windows.net/powerbi/api/.default"]


class PowerBISession:
    """Session holding Power BI connection state and auth token."""

    def __init__(self):
        self._token = None
        self._token_expires = 0
        self.group_id = POWERBI_GROUP_ID
        self.dataset_id = POWERBI_DATASET_ID

    def _ensure_token(self):
        """Ensure a valid access token exists, fetching a new one if expired."""
        if self._token and time.time() < self._token_expires - 60:
            return

        if POWERBI_AUTH_MODE == "interactive":
            self._ensure_token_interactive()
        else:
            self._ensure_token_service_principal()

    def _ensure_token_service_principal(self):
        """Fetch token via Client Credentials flow."""
        if not all([POWERBI_TENANT_ID, POWERBI_CLIENT_ID, POWERBI_CLIENT_SECRET]):
            raise ValueError(
                "Power BI Service Principal not configured: set POWERBI_TENANT_ID, POWERBI_CLIENT_ID, POWERBI_CLIENT_SECRET"
            )
        
        data = {
            "grant_type": "client_credentials",
            "client_id": POWERBI_CLIENT_ID,
            "client_secret": POWERBI_CLIENT_SECRET,
            "scope": " ".join(POWERBI_SCOPE),
        }
        r = requests.post(TOKEN_URL, data=data)
        r.raise_for_status()
        j = r.json()
        self._token = j["access_token"]
        self._token_expires = time.time() + int(j.get("expires_in", 3600))

    def _ensure_token_interactive(self):
        """Fetch token via Interactive Login flow."""
        if not all([POWERBI_TENANT_ID, POWERBI_CLIENT_ID]):
             raise ValueError(
                "Power BI Interactive Auth needs POWERBI_TENANT_ID and POWERBI_CLIENT_ID."
            )

        authority = f"https://login.microsoftonline.com/{POWERBI_TENANT_ID or 'common'}"
        
        # Use MSAL PublicClientApplication for interactive flow
        app = msal.PublicClientApplication(
            POWERBI_CLIENT_ID,
            authority=authority
        )

        # 1. Try silent first (from cache)
        accounts = app.get_accounts()
        result = None
        if accounts:
            result = app.acquire_token_silent(POWERBI_SCOPE, account=accounts[0])

        # 2. If no cache, go interactive
        if not result:
            print("\n!!! POWER BI LOGIN REQUIRED !!!")
            print("A browser window will open for authentication.")
            result = app.acquire_token_interactive(scopes=POWERBI_SCOPE)

        if "access_token" in result:
            self._token = result["access_token"]
            self._token_expires = time.time() + int(result.get("expires_in", 3600))
            print(f"✅ Authenticated as: {result.get('id_token_claims', {}).get('name', 'User')}")
        else:
            error_msg = result.get("error_description") or result.get("error")
            raise RuntimeError(f"Interactive Auth Failed: {error_msg}")

    def execute_dax(self, dax_query: str, impersonated_user: str | None = None) -> list[dict]:
        """
        Execute a DAX query and return rows as list of dicts.

        :param dax_query: DAX query, e.g. "EVALUATE Sales"
        :param impersonated_user: Optional UPN for RLS impersonation
        :return: List of row dicts (column names as keys)
        """
        self._ensure_token()
        if not self.dataset_id:
            raise ValueError("POWERBI_DATASET_ID is not set")

        if self.group_id:
            url = (
                f"https://api.powerbi.com/v1.0/myorg/groups/{self.group_id}/datasets/"
                f"{self.dataset_id}/executeQueries"
            )
        else:
            url = f"https://api.powerbi.com/v1.0/myorg/datasets/{self.dataset_id}/executeQueries"

        payload: dict[str, Any] = {
            "queries": [{"query": dax_query}],
            "serializerSettings": {"includeNulls": True},
        }
        if impersonated_user:
            payload["impersonatedUserName"] = impersonated_user

        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        
        max_retries = 3
        retry_delay = 5  # Initial delay in seconds
        
        for attempt in range(max_retries):
            logger.info(f"Executing DAX Query (Attempt {attempt + 1}/{max_retries})")
            logger.debug(f"DAX Query: {dax_query}")
            
            r = requests.post(url, json=payload, headers=headers)
            
            if r.status_code == 429:
                # Handle rate limiting
                retry_after = int(r.headers.get("Retry-After", retry_delay))
                logger.warning(f"Power BI Rate Limit Hit (429). Retrying in {retry_after}s...")
                time.sleep(retry_after)
                retry_delay *= 2  # Exponential backoff
                continue
                
            if not r.ok:
                err_body = r.text
                try:
                    err_json = r.json()
                    if "error" in err_json:
                        err_body = err_json.get("error", {}).get("message", err_body)
                    elif "message" in err_json:
                        err_body = err_json.get("message", err_body)
                except Exception:
                    pass
                
                logger.error(f"Power BI API Error ({r.status_code}): {err_body}")
                raise RuntimeError(f"Power BI executeQueries failed ({r.status_code}): {err_body}")
            
            resp = r.json()

            if "error" in resp and resp["error"]:
                logger.error(f"Power BI Response Error: {resp['error']}")
                raise RuntimeError(resp["error"].get("message", str(resp["error"])))

            results = resp.get("results", [])
            if not results:
                logger.info("Power BI returned no results.")
                return []

            first = results[0]
            if "error" in first and first["error"]:
                logger.error(f"Power BI Result Error: {first['error']}")
                raise RuntimeError(first["error"].get("message", str(first["error"])))

            tables = first.get("tables", [])
            if not tables:
                logger.info("Power BI returned no tables in result.")
                return []

            rows = tables[0].get("rows", [])
            logger.info(f"Power BI Query Succeeded. Returned {len(rows)} rows.")
            return rows
        
        raise RuntimeError(f"Power BI executeQueries failed after {max_retries} attempts due to rate limiting (429).")

    def close(self):
        """No-op; session holds no persistent connection."""
        pass


def get_powerbi_session() -> PowerBISession:
    """Return a Power BI session for executing DAX queries."""
    return PowerBISession()



# ---------------------------------------------------------------------------
# Simple in-memory TTL cache for DAX results
# Avoids redundant Power BI API round-trips for identical queries within TTL.
# Set DAX_CACHE_TTL_SECONDS=0 in env to disable.
# ---------------------------------------------------------------------------
_DAX_CACHE: dict[str, tuple[float, list]] = {}  # query -> (expires_at, rows)
_DAX_CACHE_TTL = int(os.environ.get("DAX_CACHE_TTL_SECONDS", "300"))  # 5 min default


def _cache_get(key: str) -> list | None:
    if _DAX_CACHE_TTL <= 0:
        return None
    entry = _DAX_CACHE.get(key)
    if entry and time.time() < entry[0]:
        return entry[1]
    return None


def _cache_set(key: str, value: list) -> None:
    if _DAX_CACHE_TTL > 0:
        _DAX_CACHE[key] = (time.time() + _DAX_CACHE_TTL, value)


def execute_dax(dax_query: str, impersonated_user: str | None = None) -> list[dict]:
    """Convenience: execute DAX and return rows. Results cached for DAX_CACHE_TTL_SECONDS."""
    cache_key = f"{impersonated_user}::{dax_query}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    s = get_powerbi_session()
    try:
        result = s.execute_dax(dax_query, impersonated_user)
        _cache_set(cache_key, result)
        return result
    finally:
        s.close()



def query_one_dax(dax_query: str, impersonated_user: str | None = None) -> tuple | None:
    """Execute DAX and return first row as tuple (for compatibility with query_one)."""
    rows = execute_dax(dax_query, impersonated_user)
    if not rows:
        return None
    row = rows[0]
    return tuple(row.values())


def query_all_dax(dax_query: str, impersonated_user: str | None = None) -> list[tuple]:
    """Execute DAX and return all rows as list of tuples (for compatibility with query_all)."""
    rows = execute_dax(dax_query, impersonated_user)
    return [tuple(r.values()) for r in rows]


def query_df_dax(dax_query: str, impersonated_user: str | None = None) -> pd.DataFrame:
    """Execute DAX and return a pandas DataFrame."""
    rows = execute_dax(dax_query, impersonated_user)
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def is_configured() -> bool:
    """Return True if Power BI connection is configured."""
    if POWERBI_AUTH_MODE == "interactive":
        return bool(POWERBI_TENANT_ID and POWERBI_CLIENT_ID and POWERBI_DATASET_ID)
    return bool(
        POWERBI_TENANT_ID
        and POWERBI_CLIENT_ID
        and POWERBI_CLIENT_SECRET
        and POWERBI_DATASET_ID
    )
