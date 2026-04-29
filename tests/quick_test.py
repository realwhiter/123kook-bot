import asyncio
from kook_music import MusicAPI

async def test():
    api = MusicAPI()
    result = await api.search('周杰伦', 3)
    print(f"搜索结果: {result}")

asyncio.run(test())
