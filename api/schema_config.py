"""
Semantic model schema configuration for Power BI DAX queries.

Override table and column names via environment variables if your model
uses different names (e.g. FactSales, SaleDate, NetAmount).

You can get table/column names from:
- Power BI Desktop: Model view, select a table
- DAX Studio: Connect to the model and browse
- Fabric: Semantic model settings
"""
import os


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default).strip() or default


# ----- Table names -----
# Your semantic model table names (case-sensitive in DAX)
T_FS = _env("PBI_TABLE_SALES", "fact_sales")
T_FP = _env("PBI_TABLE_PURCHASES", "fact_purchases")
T_FI = _env("PBI_TABLE_INVENTORY", "fact_inventory_snapshots")
T_DS = _env("PBI_TABLE_STORES", "dim_stores")
T_DP = _env("PBI_TABLE_PRODUCTS", "dim_products")

# ----- Sales table columns -----
C_SALE_DATE = _env("PBI_COL_SALE_DATE", "sale_date")
C_SALE_AMOUNT = _env("PBI_COL_SALE_AMOUNT", "net_amount")
C_SALE_QTY = _env("PBI_COL_SALE_QTY", "quantity")
C_SALE_IS_RETURN = _env("PBI_COL_SALE_IS_RETURN", "is_return")
C_SALE_SITE = _env("PBI_COL_SALE_SITE", "site_code")
C_SALE_BARCODE = _env("PBI_COL_SALE_BARCODE", "barcode")
C_SALE_DISCOUNT = _env("PBI_COL_SALE_DISCOUNT", "discount_pct")

# ----- Purchases table columns -----
C_PURCH_DATE = _env("PBI_COL_PURCH_DATE", "purchase_date")
C_PURCH_QTY = _env("PBI_COL_PURCH_QTY", "quantity")
C_PURCH_SITE = _env("PBI_COL_PURCH_SITE", "site_code")
C_PURCH_BARCODE = _env("PBI_COL_PURCH_BARCODE", "barcode")

# ----- Inventory table columns -----
C_INV_DATE = _env("PBI_COL_INV_DATE", "snapshot_date")
C_INV_QTY = _env("PBI_COL_INV_QTY", "quantity")
C_INV_BARCODE = _env("PBI_COL_INV_BARCODE", "barcode")
C_INV_SITE = _env("PBI_COL_INV_SITE", "site_code")

# ----- Stores table columns -----
C_STORE_SITE = _env("PBI_COL_STORE_SITE", "site_code")
C_STORE_ZONE = _env("PBI_COL_STORE_ZONE", "zone")

# ----- Products table columns -----
C_PROD_BARCODE = _env("PBI_COL_PROD_BARCODE", "barcode")
C_PROD_DEPT = _env("PBI_COL_PROD_DEPT", "department")
C_PROD_SECTION = _env("PBI_COL_PROD_SECTION", "section")

# ----- Optional: use category from sales table (Fact_Sales_Detail has DEPARTMENT, SECTION) -----
C_SALE_DEPT = _env("PBI_COL_SALE_DEPT", "")
C_SALE_SECTION = _env("PBI_COL_SALE_SECTION", "")

# ----- Purchases: if table has no date column, set PBI_PURCH_HAS_DATE=false -----
PURCH_HAS_DATE = os.environ.get("PBI_PURCH_HAS_DATE", "true").lower() in ("1", "true", "yes")

# ----- Return Flag: if stored as "Yes"/"No" text, set PBI_SALE_RETURN_VALUE_YES=Yes -----
SALE_RETURN_VALUE_YES = _env("PBI_SALE_RETURN_VALUE_YES", "")  # e.g. "Yes" for text, empty = boolean


def sale_not_return_dax(table: str, col: str) -> str:
    """DAX expression: filter to exclude returns. Supports boolean (default) or 'Yes'/'No' text (set PBI_SALE_RETURN_VALUE_YES=Yes)."""
    if SALE_RETURN_VALUE_YES:
        return f'{table}[{col}] <> "{SALE_RETURN_VALUE_YES}"'
    return f"{table}[{col}] = false()"
