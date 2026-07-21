import json
import logging
import os
import io
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from azure.identity import ClientSecretCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.filedatalake import DataLakeServiceClient
from deltalake import DeltaTable
from deltalake.writer import write_deltalake
import pandas as pd
import pyarrow as pa

logger = logging.getLogger(__name__)


def _recoverable_delta_write_error(exc: BaseException) -> bool:
    """True if clearing _delta_log and retrying the overwrite may fix the issue (incl. nested causes)."""
    parts: List[str] = []
    e: BaseException | None = exc
    for _ in range(8):
        if e is None:
            break
        parts.append(str(e))
        nxt = e.__cause__
        e = nxt if isinstance(nxt, BaseException) else None
    err = " ".join(parts).lower()
    if not err.strip():
        return False
    return (
        "invalid table" in err
        or "table version" in err
        or "invalid table version" in err
        or "version 0" in err
        or "log segment" in err
        or ("no files" in err and "log" in err)
        or ("log" in err and "commit" in err)
        or "corrupt" in err
        or "delta kernel" in err
        or "kernel error" in err
        or "generic delta" in err
        or "object store" in err
        or "objectstore" in err
    )


def _env(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value
    return default


class OneLakeJsonStore:
    """
    Stores transfer tables as JSON files inside OneLake Files area.
    This avoids SQL/ODBC while keeping data persisted in Fabric OneLake.
    """

    def __init__(self) -> None:
        self.tenant_id = _env("AZURE_TENANT_ID", "PBI_TENANT_ID", "POWERBI_TENANT_ID")
        self.client_id = _env("AZURE_CLIENT_ID", "PBI_CLIENT_ID", "POWERBI_CLIENT_ID")
        self.client_secret = _env("AZURE_CLIENT_SECRET", "PBI_CLIENT_SECRET", "POWERBI_CLIENT_SECRET")
        self.workspace = _env("FABRIC_WORKSPACE_NAME", "WS_REVENUE_ID")
        self.lakehouse = _env("FABRIC_LAKEHOUSE_NAME", default="Retail_Lakehouse")
        self.base_dir = os.environ.get("ONE_LAKE_TRANSFER_BASE_DIR", "stockpulse/transfers")

        if not all([self.tenant_id, self.client_id, self.client_secret, self.workspace, self.lakehouse]):
            raise RuntimeError(
                "OneLake config missing. Required: AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, "
                "FABRIC_WORKSPACE_NAME (or WS_REVENUE_ID), FABRIC_LAKEHOUSE_NAME."
            )

        credential = ClientSecretCredential(
            tenant_id=self.tenant_id,
            client_id=self.client_id,
            client_secret=self.client_secret,
        )
        self.service = DataLakeServiceClient(
            account_url="https://onelake.dfs.fabric.microsoft.com",
            credential=credential,
        )
        self.fs = self.service.get_file_system_client(file_system=self.workspace)

    def _path(self, table_name: str) -> str:
        return f"{self.lakehouse}.Lakehouse/Files/{self.base_dir}/{table_name}.json"

    def _delta_uri(self, table_name: str, schema: str = "dbo") -> str:
        return (
            f"abfss://{self.workspace}@onelake.dfs.fabric.microsoft.com/"
            f"{self.lakehouse}.Lakehouse/Tables/{schema}/{table_name}"
        )

    def _delta_storage_options(self) -> Dict[str, str]:
        return {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "tenant_id": self.tenant_id,
        }

    def _clear_delta_table_files(self, table_name: str, schema: str = "dbo") -> None:
        """
        Remove all *files* under a managed Delta table path (incl. _delta_log). Use when the table
        is broken (e.g. 'Invalid table version 0') so the next write_deltalake can create a fresh log.
        """
        prefix = f"{self.lakehouse}.Lakehouse/Tables/{schema}/{table_name}"
        try:
            paths = self.fs.get_paths(path=prefix, recursive=True)
        except Exception as e:
            logger.warning("OneLake: could not list %s: %s", prefix, e)
            return
        n = 0
        for p in paths:
            if getattr(p, "is_directory", False):
                continue
            name = getattr(p, "name", None) or str(p)
            if not name:
                continue
            try:
                self.fs.get_file_client(name).delete_file()
                n += 1
            except Exception as ex:
                logger.debug("OneLake: skip delete %s: %s", name, ex)
        if n:
            logger.info("OneLake: removed %d file(s) under %s for clean rewrite", n, prefix)

    # Always clear this managed folder before overwrite — avoids "No files in log segment" / broken
    # _delta_log on first write. Env ONELAKE_DELTA_CLEAR_BEFORE_WRITE adds more table names.
    _DEFAULT_PRE_CLEAR_TABLES = frozenset({"current_transfers"})
    # If Delta still fails after all retries, write these to Files/.../<name>.json (ONELAKE_DELTA_JSON_FALLBACK=0 to disable)
    _JSON_FALLBACK_TABLES = frozenset({"current_transfers"})

    def _wants_json_delta_fallback(self, table_name: str) -> bool:
        t = (table_name or "").strip().lower()
        if t not in self._JSON_FALLBACK_TABLES:
            return False
        v = (os.environ.get("ONELAKE_DELTA_JSON_FALLBACK", "1") or "1").strip().lower()
        return v not in ("0", "false", "no", "off")

    def _delete_json_sidecar_if_exists(self, table_name: str) -> None:
        """When managed Delta is healthy, drop the emergency JSON so reads use Delta, not old JSON."""
        try:
            self.fs.get_file_client(self._path(table_name)).delete_file()
        except ResourceNotFoundError:
            pass
        except Exception as ex:
            logger.debug("OneLake: could not remove JSON sidecar for %s: %s", table_name, ex)

    @classmethod
    def _wants_pre_clear_for_table(cls, table_name: str) -> bool:
        """
        If env ONELAKE_DELTA_CLEAR_BEFORE_WRITE lists this table, clear only that table's
        folder before write (e.g. current_transfers). Comma-separated, case-insensitive.
        `current_transfers` is also pre-cleared by default. Other tables in the lakehouse are
        not touched unless listed in env.
        """
        t = (table_name or "").strip().lower()
        if t and t in cls._DEFAULT_PRE_CLEAR_TABLES:
            return True
        raw = (os.environ.get("ONELAKE_DELTA_CLEAR_BEFORE_WRITE", "") or "").strip()
        if not raw:
            return False
        want = {x.strip().lower() for x in raw.split(",") if x.strip()}
        return bool(t) and t in want

    def read_delta_table(self, table_name: str, schema: str = "dbo") -> List[Dict[str, Any]]:
        """
        Read a managed OneLake Delta table and return list[dict].
        Falls back to direct parquet reading if the Delta library fails (common in Fabric).
        """
        try:
            dt = DeltaTable(
                self._delta_uri(table_name, schema=schema),
                storage_options=self._delta_storage_options(),
            )
            return dt.to_pyarrow_table().to_pylist()
        except Exception as e:
            # e.g. "No files in log segment" / invalid _delta_log — data files may still exist.
            # App UI can show rows via parquet; SQL/Spark on same path may still error until log is fixed.
            logger.debug(
                "Delta protocol read failed for %s, using parquet fallback: %s",
                table_name,
                e,
            )
            return self._read_parquet_dir(table_name, schema=schema)

    def _read_parquet_dir(self, table_name: str, schema: str = "dbo") -> List[Dict[str, Any]]:
        """
        Directly read all parquet data files in the table folder.
        """
        table_path = f"{self.lakehouse}.Lakehouse/Tables/{schema}/{table_name}"
        try:
            paths = self.fs.get_paths(path=table_path, recursive=True)
            all_rows = []
            for p in paths:
                if p.name.lower().endswith(".parquet") and "_delta_log" not in p.name:
                    file_client = self.fs.get_file_client(p.name)
                    content = file_client.download_file().readall()
                    # Read into pandas and convert to list of dicts
                    df = pd.read_parquet(io.BytesIO(content))
                    all_rows.extend(df.to_dict(orient='records'))
            return all_rows
        except Exception:
            return []

    def replace_delta_table(self, table_name: str, rows: List[Dict[str, Any]], schema: str = "dbo") -> None:
        """
        Overwrite managed OneLake Delta table with provided rows.
        Only the folder Tables/{schema}/{table_name} is ever modified — never the whole lakehouse.
        If the Delta log is corrupt (e.g. 'No files in log segment', 'Invalid table version 0'),
        the table path is cleared and the write is retried (up to 3 attempts). If the last attempt still
        fails, `current_transfers` can be written to `Files/.../current_transfers.json` (emergency) unless
        ONELAKE_DELTA_JSON_FALLBACK=0. A successful Delta write removes that JSON so reads prefer Delta.
        `current_transfers` is pre-cleared by default before the first write; add more table names via
        ONELAKE_DELTA_CLEAR_BEFORE_WRITE (comma-separated).
        """
        max_write_attempts = 3
        uri = self._delta_uri(table_name, schema=schema)
        storage_options = self._delta_storage_options()
        for write_attempt in range(1, max_write_attempts + 1):
            if write_attempt == 1:
                if self._wants_pre_clear_for_table(table_name):
                    self._clear_delta_table_files(table_name, schema=schema)
            else:
                self._clear_delta_table_files(table_name, schema=schema)

            existing_schema = None
            existing_columns: List[str] = []
            try:
                existing_table = DeltaTable(uri, storage_options=storage_options).to_pyarrow_table()
                existing_schema = existing_table.schema
                existing_columns = list(existing_table.column_names)
            except Exception as e:
                logger.debug("DeltaTable schema probe failed for %s (ok if log incomplete): %s", table_name, e)
                existing_schema = None
                existing_columns = []

            # If table already exists, strictly align incoming rows to existing schema.
            if existing_schema is not None and existing_columns:
                clipped_rows = []
                for row in rows or []:
                    # Map incoming keys case-insensitively to existing Delta schema columns.
                    # This prevents data loss when app rows use lower-case keys but table has
                    # mixed/upper-case names.
                    row_ci = {str(key).lower(): value for key, value in (row or {}).items()}
                    clipped_rows.append({k: row_ci.get(str(k).lower()) for k in existing_columns})
                df = pd.DataFrame(clipped_rows)
                for col in existing_columns:
                    if col not in df.columns:
                        df[col] = None
                df = df[existing_columns]
                table = pa.Table.from_pandas(df, preserve_index=False)
                aligned_arrays = []
                for field in existing_schema:
                    arr = table.column(field.name) if field.name in table.column_names else pa.nulls(table.num_rows, type=field.type)
                    try:
                        arr = arr.cast(field.type)
                    except Exception:
                        arr = pa.nulls(table.num_rows, type=field.type)
                    aligned_arrays.append(arr)
                table = pa.Table.from_arrays(aligned_arrays, schema=existing_schema)
            else:
                df = pd.DataFrame(rows or [])
                if df.empty:
                    df = pd.DataFrame({"_empty": pd.Series(dtype="string")})
                table = pa.Table.from_pandas(df, preserve_index=False)
            try:
                try:
                    write_deltalake(
                        uri,
                        table,
                        mode="overwrite",
                        storage_options=storage_options,
                        schema_mode="overwrite",
                    )
                except TypeError:
                    # Older deltalake: no schema_mode argument
                    write_deltalake(
                        uri,
                        table,
                        mode="overwrite",
                        storage_options=storage_options,
                    )
                self._delete_json_sidecar_if_exists(table_name)
                return
            except Exception as e:
                if _recoverable_delta_write_error(e) and write_attempt < max_write_attempts:
                    logger.warning(
                        "Delta write failed for %s (attempt %s/%s): %s — clearing table path and retrying",
                        table_name,
                        write_attempt,
                        max_write_attempts,
                        e,
                    )
                    continue
                # Last write attempt: persist to Files JSON so create / approve is not blocked
                # (e.g. Generic delta kernel: No files in log segment) — UI reads merge empty Delta + JSON.
                if write_attempt == max_write_attempts and self._wants_json_delta_fallback(table_name):
                    logger.error(
                        "Delta write failed for %s after %s attempt(s); saving to OneLake Files JSON. Error: %s",
                        table_name,
                        max_write_attempts,
                        e,
                    )
                    self.replace_table(table_name, list(rows or []))
                    # Note: API `_read_transfer_table` uses managed Delta first; JSON is only used if Delta read raises.
                    return
                raise

    def read_table(self, table_name: str) -> List[Dict[str, Any]]:
        file_client = self.fs.get_file_client(self._path(table_name))
        try:
            content = file_client.download_file().readall()
        except ResourceNotFoundError:
            return []
        if not content:
            return []
        parsed = json.loads(content.decode("utf-8"))
        if isinstance(parsed, list):
            return parsed
        return []

    def write_table(self, table_name: str, rows: List[Dict[str, Any]]) -> None:
        payload = json.dumps(rows, ensure_ascii=True, default=str).encode("utf-8")
        file_client = self.fs.get_file_client(self._path(table_name))
        file_client.create_file()
        if payload:
            file_client.append_data(payload, 0, len(payload))
        file_client.flush_data(len(payload))

    def replace_table(self, table_name: str, rows: List[Dict[str, Any]]) -> None:
        file_client = self.fs.get_file_client(self._path(table_name))
        try:
            file_client.delete_file()
        except ResourceNotFoundError:
            pass
        self.write_table(table_name, rows)

    def list_by_filter(
        self,
        table_name: str,
        donor_site: Optional[str] = None,
        donor_sku: Optional[str] = None,
        recv_site: Optional[str] = None,
        recv_sku: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        rows = self.read_table(table_name)

        def ok(v: Optional[str], row_val: Any) -> bool:
            if not v or v in ("All Sites", "All Stores", "All", "All SKUs"):
                return True
            return str(row_val or "").strip() == str(v).strip()

        filtered = []
        for row in rows:
            if not ok(donor_site, row.get("donor_store")):
                continue
            if not ok(donor_sku, row.get("sku")):
                continue
            if not ok(recv_site, row.get("receiver_store")):
                continue
            if not ok(recv_sku, row.get("sku")):
                continue
            filtered.append(row)
        return filtered

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

