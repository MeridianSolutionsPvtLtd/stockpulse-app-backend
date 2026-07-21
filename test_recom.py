import asyncio
from api.routes.replenishments_recom import get_recommendations

async def test():
    # Test with default params
    res = await get_recommendations()
    print(f"Total: {res.get('total')}")
    print(f"Data length: {len(res.get('data', []))}")
    if res.get('error'):
        print(f"Error: {res['error']}")
    if len(res.get('data', [])) > 0:
        print(f"First row: {res['data'][0]}")

asyncio.run(test())
