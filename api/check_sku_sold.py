import sys
import os

# Set up environment to import application modules
sys.path.append(os.getcwd())

from api.db_powerbi import execute_dax

def check_sku_sold():
    query = """
    EVALUATE
    SUMMARIZECOLUMNS(
        'Fact_Sales_Detail'[SECTION],
        'Fact_Sales_Detail'[DEPARTMENT],
        'Fact_Sales_Detail'[Color],
        'Fact_Sales_Detail'[Fabric],
        'Fact_Sales_Detail'[Size],
        "total_sold", [Qty_Sold]
    )
    """
    rows = execute_dax(query)
    
    found = False
    for r in (rows or []):
        section = r.get("Fact_Sales_Detail[SECTION]")
        dept = r.get("Fact_Sales_Detail[DEPARTMENT]")
        color = r.get("Fact_Sales_Detail[Color]")
        fabric = r.get("Fact_Sales_Detail[Fabric]")
        size = r.get("Fact_Sales_Detail[Size]")
        sold = r.get("[total_sold]")
        
        if (str(section).lower() == "top" and 
            str(dept).lower() == "top" and 
            str(color).lower() == "white" and 
            str(fabric).lower() == "polyester" and 
            str(size).lower() == "xxl"):
            print(f"MATCH FOUND:")
            print(f"Section: {section}")
            print(f"Dept: {dept}")
            print(f"Color: {color}")
            print(f"Fabric: {fabric}")
            print(f"Size: {size}")
            print(f"TOTAL SOLD: {sold}")
            found = True
            break
            
    if not found:
        print("No matching record found for the specified critera.")

if __name__ == "__main__":
    check_sku_sold()
