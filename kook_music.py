"""
Kook 机器人音乐播放模块
功能：真正的音频播放，使用网易云音乐API + ffmpeg RTP推流
"""
import asyncio
import logging
import subprocess
import json
import re
import os
import threading
from khl import Message
import khl.api as khl_api
from typing import Optional, List, Dict
import aiohttp

logger = logging.getLogger(__name__)

music_player = None

class MusicPlayer:
    """音乐播放器类"""
    
    def __init__(self):
        self.current_channel_id = None
        self.current_guild_id = None
        self.playlist: List[Dict] = []
        self.current_index = 0
        self.is_playing = False
        self.is_paused = False
        self.process: Optional[subprocess.Popen] = None
        self.keep_alive_task = None
        self.voice_info = None
        self.monitor_task = None
    
    async def join_channel(self, bot, guild_id: str, channel_id: str, channel_name: str) -> bool:
        """让机器人加入语音频道"""
        import traceback
        import datetime
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"🎵 [DEBUG-MUSIC-JOIN] [{current_time}] 开始执行 join_channel, channel_id={channel_id}, channel_name={channel_name}")
        
        try:
            logger.info(f"🎵 [DEBUG-MUSIC-JOIN] [{current_time}] 调用 khl_api.Voice.join")
            result = await bot.client.gate.exec_req(
                khl_api.Voice.join(channel_id=channel_id)
            )
            logger.info(f"🎵 [DEBUG-MUSIC-JOIN] [{current_time}] join 成功返回: {result}")
            
            self.voice_info = result
            self.current_channel_id = channel_id
            self.current_guild_id = guild_id
            
            logger.info(f"🎵 加入语音频道成功: {channel_name}, 推流信息: {result}")
            return True
        except Exception as e:
            logger.error(f"🎵 [DEBUG-MUSIC-JOIN] [{current_time}] 加入语音频道失败: {e}")
            logger.error(f"🎵 [DEBUG-MUSIC-JOIN] [{current_time}] 完整堆栈: {traceback.format_exc()}")
            return False
    
    async def leave_channel(self, bot) -> bool:
        """让机器人离开语音频道"""
        import traceback
        import datetime
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"🎵 [DEBUG-MUSIC-LEAVE] [{current_time}] 开始执行 leave_channel, channel_id={self.current_channel_id}")
        logger.info(f"🎵 [DEBUG-MUSIC-LEAVE] [{current_time}] 调用栈: {traceback.format_stack()}")
        
        try:
            if self.current_channel_id:
                logger.info(f"🎵 [DEBUG-MUSIC-LEAVE] [{current_time}] 调用 khl_api.Voice.leave")
                await bot.client.gate.exec_req(
                    khl_api.Voice.leave(channel_id=self.current_channel_id)
                )
                logger.info(f"🎵 [DEBUG-MUSIC-LEAVE] [{current_time}] leave 成功")
                self.stop()
                self.current_channel_id = None
                self.current_guild_id = None
                logger.info("🎵 已离开语音频道")
                return True
        except Exception as e:
            logger.error(f"🎵 [DEBUG-MUSIC-LEAVE] [{current_time}] 离开语音频道失败: {e}")
            logger.error(f"🎵 [DEBUG-MUSIC-LEAVE] [{current_time}] 完整堆栈: {traceback.format_exc()}")
            return False
    
    def stop(self):
        """停止播放"""
        self.is_playing = False
        self.is_paused = False
        self.playlist = []
        self.current_index = 0
        
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except:
                pass
            self.process = None
        
        if self.keep_alive_task:
            self.keep_alive_task.cancel()
            self.keep_alive_task = None
        
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
    
    async def keep_alive(self, bot):
        """保持语音连接活跃"""
        import datetime
        current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"🎵 [DEBUG-KEEPALIVE] [{current_time_str}] 开始保持连接, is_playing={self.is_playing}, channel_id={self.current_channel_id}")
        
        try:
            while self.is_playing and self.current_channel_id:
                await asyncio.sleep(10)
                
                current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                if self.process and self.process.poll() is not None:
                    logger.error(f"🎵 [DEBUG-KEEPALIVE] [{current_time_str}] FFmpeg进程已退出，退出码: {self.process.returncode}")
                
                logger.info(f"🎵 [DEBUG-KEEPALIVE] [{current_time_str}] 语音连接保持中, is_playing={self.is_playing}")
                
                if self.process and self.process.poll() is None:
                    logger.info(f"🎵 [DEBUG-KEEPALIVE] [{current_time_str}] FFmpeg进程正常运行，PID={self.process.pid}")
                else:
                    logger.warning(f"🎵 [DEBUG-KEEPALIVE] [{current_time_str}] FFmpeg进程状态异常")
                    
        except asyncio.CancelledError:
            logger.info("🎵 保持连接任务已取消")
        except Exception as e:
            logger.error(f"❌ 保持连接失败: {e}")
    
    async def play_next(self, bot):
        """播放下一首"""
        if not self.playlist or self.current_index >= len(self.playlist):
            await self.leave_channel(bot)
            return None
        
        song = self.playlist[self.current_index]
        logger.info(f"🎵 准备播放: {song['name']} - {song['artist']}")
        
        return song
    
    async def play(self, bot, msg: Message, song: Dict):
        """播放音乐"""
        if not self.current_channel_id:
            return False, "机器人未加入语音频道"
        
        song_url = song.get('url')
        if not song_url:
            return False, "无法获取歌曲播放链接"
        
        logger.info(f"🎵 获取音乐播放链接: {song['name']}")
        
        song_url = song.get('url', '')
        if not song_url:
            play_url = await music_api.get_play_url(song.get('id', 0))
            if not play_url:
                logger.warning(f"⚠️ 无法获取播放链接: {song['name']}")
                return False, f"歌曲《{song['name']}》暂时无法播放，请尝试其他歌曲"
            song_url = play_url
        
        logger.info(f"✅ 获取到播放链接: {song_url[:50]}...")
        
        if self.process and self.process.poll() is None:
            logger.info("🎵 停止当前播放，准备播放新歌曲")
            self.is_playing = False
            if self.keep_alive_task:
                self.keep_alive_task.cancel()
                self.keep_alive_task = None
            if self.monitor_task:
                self.monitor_task.cancel()
                self.monitor_task = None
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except:
                self.process.kill()
            await asyncio.sleep(0.5)
        
        try:
            voice_result = await bot.client.gate.exec_req(
                khl_api.Voice.join(channel_id=self.current_channel_id)
            )
            self.voice_info = voice_result
            logger.info(f"🎵 获取新推流地址成功: {voice_result}")
        except Exception as e:
            logger.error(f"❌ 获取新推流地址失败: {e}")
            if not self.voice_info:
                return False, "无法获取推流地址，请重新加入语音频道"
            logger.warning("⚠️ 推流地址可能已过期，尝试重新加入频道...")
            try:
                await bot.client.gate.exec_req(khl_api.Voice.leave(channel_id=self.current_channel_id))
            except:
                pass
            await asyncio.sleep(1)
            try:
                voice_result = await bot.client.gate.exec_req(
                    khl_api.Voice.join(channel_id=self.current_channel_id)
                )
                self.voice_info = voice_result
                logger.info(f"🎵 重新获取推流地址成功: {voice_result}")
            except Exception as e2:
                logger.error(f"❌ 重新获取推流地址失败: {e2}")
                return False, "推流地址已过期，请使用 /join 命令重新加入语音频道"
        
        self.is_playing = True
        self.is_paused = False
        
        try:
            voice_info = self.voice_info
            ip = voice_info.get('ip')
            port = voice_info.get('port')
            audio_ssrc = voice_info.get('audio_ssrc', '1111')
            audio_pt = voice_info.get('audio_pt', '111')
            rtcp_mux = voice_info.get('rtcp_mux', True)
            
            rtp_url = f"rtp://{ip}:{port}"
            if not rtcp_mux and 'rtcp_port' in voice_info:
                rtp_url += f"?rtcpport={voice_info.get('rtcp_port')}"
            
            ffmpeg_cmd = [
                'ffmpeg',
                '-re',
                '-i', song_url,
                '-map', '0:a',
                '-acodec', 'libopus',
                '-ab', '48k',
                '-ac', '2',
                '-ar', '48000',
                '-filter:a', 'volume=0.8',
                '-f', 'rtp',
                '-ssrc', str(audio_ssrc),
                '-payload_type', str(audio_pt),
                rtp_url
            ]
            
            logger.info(f"🎵 FFmpeg命令: {' '.join(ffmpeg_cmd)}")
            logger.info(f"🎵 推流地址: {rtp_url}")
            logger.info(f"🎵 语音信息: IP={ip}, Port={port}, SSRC={audio_ssrc}, PT={audio_pt}")
            
            self.process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            await asyncio.sleep(0.5)
            if self.process.poll() is not None:
                stderr_output = self.process.stderr.read().decode('utf-8', errors='ignore') if self.process.stderr else ''
                logger.error(f"❌ FFmpeg进程启动失败，退出码: {self.process.returncode}")
                logger.error(f"❌ FFmpeg错误输出: {stderr_output}")
                self.is_playing = False
                return False, f"FFmpeg启动失败"
            
            await asyncio.sleep(2)
            
            if self.process.poll() is not None:
                stdout, stderr = self.process.communicate()
                stderr_text = stderr.decode('utf-8', errors='ignore')
                logger.error(f"❌ FFmpeg进程已退出，退出码: {self.process.returncode}")
                logger.error(f"❌ FFmpeg完整错误输出:\n{stderr_text}")
                self.is_playing = False
                return False, f"FFmpeg启动失败: {stderr_text[-200:]}"
            
            self.keep_alive_task = asyncio.create_task(self.keep_alive(bot))
            self.monitor_task = asyncio.create_task(self.monitor_playback(bot))
            
            logger.info(f"🎵 开始播放: {song['name']} - {song['artist']}")
            logger.info(f"🎵 FFmpeg进程ID: {self.process.pid}")
            return True, f"正在播放: {song['name']} - {song['artist']}"
            
        except Exception as e:
            self.is_playing = False
            logger.error(f"❌ 播放失败: {e}")
            return False, f"播放失败: {str(e)}"
    
    async def monitor_playback(self, bot):
        """监控播放状态，播放完成后自动播放下一首"""
        try:
            while self.process and self.process.poll() is None:
                await asyncio.sleep(1)
            
            if self.is_playing and self.process:
                exit_code = self.process.returncode
                if exit_code == 0:
                    logger.info("🎵 当前歌曲播放完成")
                else:
                    logger.warning(f"⚠️ 播放进程异常退出，退出码: {exit_code}")
                
                self.current_index += 1
                
                if self.current_index < len(self.playlist):
                    next_song = self.playlist[self.current_index]
                    logger.info(f"🎵 尝试播放下一首: {next_song['name']}")
                    success, status = await self.play(bot, None, next_song)
                    if not success:
                        logger.warning(f"⚠️ 播放失败，跳过: {status}")
                        await self.monitor_playback(bot)
                else:
                    logger.info("🎵 播放列表已全部播放完成")
                    self.is_playing = False
        except asyncio.CancelledError:
            logger.info("🎵 播放监控任务已取消")
        except Exception as e:
            logger.error(f"❌ 监控播放失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    def pause(self):
        """暂停播放"""
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.is_paused = True
            return True
        return False
    
    def resume(self, bot):
        """继续播放"""
        if self.is_paused and self.current_channel_id:
            self.is_paused = False
            return True
        return False
    
    def skip(self):
        """跳过当前歌曲"""
        if self.process and self.process.poll() is None:
            self.process.terminate()
        if self.current_index < len(self.playlist) - 1:
            self.current_index += 1
            return True
        return False
    
    def get_status(self) -> str:
        """获取播放状态"""
        if not self.is_playing:
            return "未在播放"
        if self.is_paused:
            return "已暂停"
        if self.playlist and self.current_index < len(self.playlist):
            song = self.playlist[self.current_index]
            return f"正在播放: {song['name']} - {song['artist']}"
        return "未知状态"


class MusicAPI:
    """网易云音乐API"""
    
    def __init__(self):
        self.search_url = "https://music.163.com/api/cloudsearch/pc"
        self.play_url_base = "https://music.163.com"
    
    async def search(self, keyword: str, limit: int = 5) -> List[Dict]:
        """搜索歌曲"""
        try:
            params = {
                's': keyword,
                'type': 1,
                'limit': limit,
                'offset': 0
            }
            
            logger.info(f"🎵 搜索: {keyword}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.search_url,
                    params=params,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Referer': 'https://music.163.com/',
                        'Accept': '*/*',
                        'Accept-Language': 'zh-CN,zh;q=0.9'
                    },
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    logger.info(f"搜索响应状态: {resp.status}")
                    text = await resp.text()
                    
                    try:
                        result = json.loads(text)
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ 返回不是JSON: {text[:200]}")
                        return []
                    
                    logger.info(f"搜索结果: {result}")
                    
                    songs = []
                    for item in result.get('result', {}).get('songs', []):
                        fee = item.get('fee', 0)
                        if fee == 1:
                            continue
                        
                        artists_list = item.get('artists', [])
                        if not artists_list:
                            artists_list = item.get('ar', [])
                        artists = ', '.join([a.get('name', '') for a in artists_list])
                        songs.append({
                            'id': item.get('id'),
                            'name': item.get('name'),
                            'artist': artists if artists else '未知',
                            'album': item.get('album', {}).get('name', '') if item.get('album') else '',
                            'duration': item.get('duration', 0)
                        })
                    
                    logger.info(f"✅ 搜索完成，返回 {len(songs)} 首歌曲")
                    return songs
        except Exception as e:
            logger.error(f"❌ 搜索失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []
    
    async def get_play_url(self, song_id: int) -> Optional[str]:
        """获取歌曲播放链接"""
        try:
            play_url = f"{self.play_url_base}/song/media/outer/url?id={song_id}"
            
            logger.info(f"🎵 获取播放链接: {play_url}")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    play_url,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                        'Referer': 'https://music.163.com/'
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                    allow_redirects=True
                ) as resp:
                    final_url = str(resp.url)
                    logger.info(f"✅ 获取播放链接成功: {final_url[:50]}...")
                    return final_url
        except Exception as e:
            logger.error(f"❌ 获取播放链接失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    async def get_song_details(self, song_id: int) -> Optional[Dict]:
        """获取歌曲详细信息"""
        try:
            api_url = "https://music.163.com/api/song/detail"
            params = {'id': song_id, 'ids': f'[{song_id}]'}
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    api_url,
                    params=params,
                    headers={'User-Agent': 'Mozilla/5.0'}
                ) as resp:
                    result = await resp.json()
                    songs = result.get('songs', [])
                    if songs:
                        song = songs[0]
                        artists = ', '.join([a['name'] for a in song.get('artists', [])])
                        return {
                            'id': song.get('id'),
                            'name': song.get('name'),
                            'artist': artists,
                            'album': song.get('album', {}).get('name', ''),
                            'url': f"https://music.163.com/song/media/outer/url?id={song_id}"
                        }
        except Exception as e:
            logger.error(f"❌ 获取歌曲详情失败: {e}")
        return None


music_api = MusicAPI()

music_selections = {}

def set_music_player_info(voice_info: dict, channel_id: str, guild_id: str):
    """设置音乐播放器的频道信息"""
    global music_player
    if music_player is None:
        music_player = MusicPlayer()
    music_player.voice_info = voice_info
    music_player.current_channel_id = channel_id
    music_player.current_guild_id = guild_id
    logger.info(f"🎵 已更新音乐播放器频道信息: {channel_id}")

async def handle_music_command(msg: Message, bot, voice_api):
    """处理用户的音乐命令"""
    user_id = msg.author_id
    message_type = type(msg).__name__
    
    global music_player
    if music_player is None:
        music_player = MusicPlayer()
    
    if message_type == 'PrivateMessage':
        await msg.reply("❌ 请在服务器群里 @机器人 使用此命令，私聊无法播放音乐哦～")
        return
    
    try:
        guild_id = msg.guild.id
    except Exception:
        guild_id = None
    
    if not guild_id:
        await msg.reply("❌ 无法获取服务器信息，请确保在服务器中使用此命令")
        return
    
    try:
        result = await bot.client.gate.exec_req(khl_api.Voice.list())
        voice_channels = result.get('items', [])
        
        if not voice_channels:
            await msg.reply("❌ 机器人当前不在语音频道中，无法播放音乐哦！\n\n请先使用 `/join` 命令让我进入语音频道，然后再播放音乐吧～")
            return
        
        voice_channel = voice_channels[0]
        channel_id = voice_channel.get('id')
        channel_name = voice_channel.get('name', '语音频道')
        
    except Exception as e:
        logger.error(f"❌ 检查语音频道失败: {e}")
        await msg.reply("❌ 无法获取语音频道状态，请确保机器人已在语音频道中！\n\n请先使用 `/join` 命令让我进入语音频道，然后再播放音乐吧～")
        return
    
    music_selections[user_id] = {
        'guild_id': guild_id,
        'step': 'waiting_keyword'
    }
    
    await msg.reply("🎵 好的，让我来帮你播放音乐！\n\n请输入要搜索的歌曲名或歌手名")
    logger.info(f"🎵 已向用户 {user_id} 发送搜索提示")


async def handle_music_input(msg: Message, bot, voice_api):
    """处理用户的音乐输入"""
    user_id = msg.author_id
    content = msg.content.strip()
    
    global music_player
    if music_player is None:
        music_player = MusicPlayer()
    
    if user_id not in music_selections:
        return False
    
    selection = music_selections[user_id]
    step = selection.get('step')
    
    if step == 'waiting_keyword':
        songs = await music_api.search(content, limit=5)
        
        if not songs:
            await msg.reply("❌ 未找到相关歌曲，请换个关键词试试")
            return True
        
        selection['songs'] = songs
        selection['step'] = 'waiting_choice'
        
        song_list = "🔍 搜索结果：\n\n"
        for i, song in enumerate(songs, 1):
            duration = song.get('duration', 0) // 1000
            minutes = duration // 60
            seconds = duration % 60
            song_list += f"{i}. {song['name']} - {song['artist']} ({minutes:02d}:{seconds:02d})\n"
        
        song_list += "\n请回复数字编号选择歌曲（如：1）"
        await msg.reply(song_list)
        return True
    
    elif step == 'waiting_choice':
        try:
            choice = int(content)
            songs = selection.get('songs', [])
            
            if choice < 1 or choice > len(songs):
                await msg.reply("❌ 输入无效，请回复有效的数字编号")
                return True
            
            selected_song = songs[choice - 1]
            
            song_details = {
                'id': selected_song['id'],
                'name': selected_song['name'],
                'artist': selected_song['artist'],
                'album': selected_song.get('album', ''),
                'url': f"https://music.163.com/song/media/outer/url?id={selected_song['id']}"
            }
            
            music_player.playlist = [song_details]
            music_player.current_index = 0
            music_player.current_guild_id = selection.get('guild_id')
            
            success, status = await music_player.play(bot, msg, song_details)
            
            if success:
                await msg.reply(f"🎵 {status}")
            else:
                await msg.reply(f"❌ {status}")
            
        except ValueError:
            await msg.reply("❌ 请输入数字编号（如：1）")
            return True
        except Exception as e:
            logger.error(f"❌ 播放失败: {e}")
            await msg.reply(f"❌ 播放失败: {str(e)}")
        
        if user_id in music_selections:
            del music_selections[user_id]
        return True
    
    return False


def is_in_music_selection(user_id: str) -> bool:
    """检查用户是否在音乐选择状态中"""
    return user_id in music_selections


async def handle_music_control(msg: Message, bot, voice_api, content: str):
    """处理音乐控制命令"""
    global music_player
    
    if music_player is None:
        music_player = MusicPlayer()
    
    if content in ['music_stop', '/music_stop', '停止播放', 'stop']:
        music_player.stop()
        if music_player.current_channel_id:
            await music_player.leave_channel(bot)
        await msg.reply("⏹️ 已停止播放并离开频道")
        return True
    
    elif content in ['music_pause', '/music_pause', '暂停', 'pause']:
        if music_player.pause():
            await msg.reply("⏸️ 已暂停播放")
        else:
            await msg.reply("❌ 当前没有在播放")
        return True
    
    elif content in ['music_resume', '/music_resume', '继续', 'resume']:
        if music_player.resume(bot):
            await msg.reply("▶️ 已继续播放")
        else:
            await msg.reply("❌ 当前未暂停")
        return True
    
    elif content in ['music_skip', '/music_skip', '下一首', 'skip']:
        if music_player.skip():
            await msg.reply("⏭️ 已切换到下一首")
        else:
            await msg.reply("❌ 没有下一首了")
        return True
    
    elif content in ['music_status', '/music_status', '播放状态', 'status']:
        status = music_player.get_status()
        await msg.reply(f"🎵 当前状态: {status}")
        return True
    
    return False
