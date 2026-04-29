#!/usr/bin/env python3
"""
测试音乐搜索功能
"""
import asyncio
import logging
from kook_music import MusicAPI

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s'
)
logger = logging.getLogger(__name__)

async def test_music_search():
    """测试音乐搜索功能"""
    logger.info("🎵 开始测试音乐搜索功能")
    
    music_api = MusicAPI()
    
    # 测试搜索
    test_keywords = ["周杰伦", "陈奕迅", "Taylor Swift"]
    
    for keyword in test_keywords:
        logger.info(f"🔍 搜索关键词: {keyword}")
        songs = await music_api.search(keyword, limit=3)
        
        if songs:
            logger.info(f"✅ 搜索成功，找到 {len(songs)} 首歌曲")
            for i, song in enumerate(songs, 1):
                logger.info(f"   {i}. {song['name']} - {song['artist']}")
        else:
            logger.error(f"❌ 搜索失败，未找到歌曲")
        
        logger.info("-" * 50)
    
    logger.info("🎵 音乐搜索功能测试完成")

if __name__ == "__main__":
    asyncio.run(test_music_search())
