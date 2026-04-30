"""Kook 机器人音乐播放模块。

- 网易云音乐搜索(自动过滤付费/灰色歌曲)
- ffmpeg RTP 推流到 KOOK 语音频道
- 多曲队列 / 自动连播 / 跳过 / 真暂停继续

暂停继续的实现:Windows 上没有 SIGSTOP/SIGCONT,所以 pause 是
"杀 ffmpeg + 记录已播 wall-clock 时长",resume 用 ffmpeg -ss <offset>
重起。代价是 1-2s 断点和粗略的位置精度(因为 -re 让 ffmpeg
按实时速率读输入,wall clock ≈ playback offset)。
"""
import asyncio
import json
import logging
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import aiohttp
import khl.api as khl_api
from khl import Message

logger = logging.getLogger(__name__)

# 模块级单例:由 _ensure_player() 惰性创建
music_player: Optional["MusicPlayer"] = None

# {user_id: {guild_id, step: 'waiting_keyword'|'waiting_choice', songs?}}
music_selections: Dict[str, dict] = {}


class MusicPlayer:
    def __init__(self):
        # 频道
        self.current_channel_id: Optional[str] = None
        self.current_guild_id: Optional[str] = None
        # 队列
        self.playlist: List[Dict] = []
        self.current_index: int = 0
        # 状态
        self.is_playing: bool = False
        self.is_paused: bool = False
        # ffmpeg 进程 + 推流端点
        self.process: Optional[subprocess.Popen] = None
        self.voice_info: Optional[dict] = None
        self.voice_info_used: bool = False
        # 异步任务
        self.monitor_task: Optional[asyncio.Task] = None
        # 播放进度估算(wall clock):用于 pause 时存 offset
        self._play_started_at: Optional[float] = None
        self._paused_offset_ms: int = 0

    # ---------- 推流端点 ----------
    async def _refresh_voice_endpoint(self, bot) -> bool:
        """leave + 重新 join 拿新推流地址。失败返回 False。"""
        if not self.current_channel_id:
            return False
        try:
            await bot.client.gate.exec_req(
                khl_api.Voice.leave(channel_id=self.current_channel_id))
        except Exception as e:
            logger.debug(f"leave 失败(可忽略): {e}")
        await asyncio.sleep(1)
        try:
            result = await bot.client.gate.exec_req(
                khl_api.Voice.join(channel_id=self.current_channel_id))
            self.voice_info = result
            self.voice_info_used = False
            logger.info("🎵 推流地址已刷新")
            return True
        except Exception as e:
            logger.error(f"❌ 推流地址刷新失败: {e}")
            return False

    async def _ensure_voice_endpoint(self, bot) -> bool:
        """首次有可用 voice_info 直接复用,否则刷新。"""
        if self.voice_info and not self.voice_info_used:
            logger.debug("复用现有推流地址")
            return True
        return await self._refresh_voice_endpoint(bot)

    # ---------- 频道 ----------
    async def join_channel(self, bot, guild_id, channel_id, channel_name) -> bool:
        try:
            result = await bot.client.gate.exec_req(
                khl_api.Voice.join(channel_id=channel_id))
            self.voice_info = result
            self.voice_info_used = False
            self.current_channel_id = channel_id
            self.current_guild_id = guild_id
            logger.info(f"🎵 加入语音频道: {channel_name}")
            return True
        except Exception as e:
            logger.error(f"🎵 加入语音频道失败: {e}")
            return False

    async def leave_channel(self, bot) -> bool:
        try:
            if self.current_channel_id:
                await bot.client.gate.exec_req(
                    khl_api.Voice.leave(channel_id=self.current_channel_id))
                self.stop()
                self.current_channel_id = None
                self.current_guild_id = None
                self.voice_info = None
                self.voice_info_used = False
                logger.info("🎵 已离开语音频道")
                return True
        except Exception as e:
            logger.error(f"🎵 离开语音频道失败: {e}")
        return False

    # ---------- ffmpeg ----------
    def _build_ffmpeg_cmd(self, song_url: str, voice_info: dict,
                          offset_ms: int = 0) -> List[str]:
        ip = voice_info.get('ip')
        port = voice_info.get('port')
        ssrc = voice_info.get('audio_ssrc', '1111')
        pt = voice_info.get('audio_pt', '111')
        rtcp_mux = voice_info.get('rtcp_mux', True)

        rtp_url = f"rtp://{ip}:{port}"
        if not rtcp_mux and 'rtcp_port' in voice_info:
            rtp_url += f"?rtcpport={voice_info.get('rtcp_port')}"

        cmd = ['ffmpeg']
        # -ss 放在 -i 前面(input seek)更快、精度足够
        if offset_ms > 0:
            cmd += ['-ss', f"{offset_ms / 1000:.2f}"]
        cmd += [
            '-re', '-i', song_url,
            '-map', '0:a',
            '-acodec', 'libopus', '-ab', '48k',
            '-ac', '2', '-ar', '48000',
            '-filter:a', 'volume=0.8',
            '-f', 'rtp',
            '-ssrc', str(ssrc),
            '-payload_type', str(pt),
            rtp_url,
        ]
        return cmd

    def _spawn_ffmpeg(self, song_url: str, voice_info: dict,
                      offset_ms: int = 0) -> Optional[subprocess.Popen]:
        cmd = self._build_ffmpeg_cmd(song_url, voice_info, offset_ms)
        logger.debug(f"FFmpeg cmd: {' '.join(cmd)}")
        try:
            return subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        except Exception as e:
            logger.error(f"❌ FFmpeg 启动异常: {e}")
            return None

    async def _wait_ffmpeg_alive(self, proc: subprocess.Popen) -> Tuple[bool, str]:
        """启动后短暂等待,确认 ffmpeg 没立刻挂掉。"""
        await asyncio.sleep(0.5)
        if proc.poll() is not None:
            stderr = (proc.stderr.read().decode('utf-8', errors='ignore')
                      if proc.stderr else '')
            return False, f"FFmpeg 启动失败: {stderr[-200:]}"
        await asyncio.sleep(2)
        if proc.poll() is not None:
            stderr = (proc.stderr.read().decode('utf-8', errors='ignore')
                      if proc.stderr else '')
            return False, f"FFmpeg 启动后退出: {stderr[-200:]}"
        return True, ""

    def _terminate_process(self):
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except Exception:
                try:
                    self.process.kill()
                except Exception:
                    pass
        self.process = None

    async def _start_playback(self, bot, song_url: str,
                              offset_ms: int = 0) -> Tuple[bool, str]:
        """启动一次播放(含一次失败重试):
        1. 确保推流端点
        2. 启 ffmpeg 并校验存活
        3. 失败时刷新推流端点重试一次
        """
        if not await self._ensure_voice_endpoint(bot):
            return False, "推流地址获取失败,请重新加入语音频道"

        proc = self._spawn_ffmpeg(song_url, self.voice_info, offset_ms)
        if proc is None:
            return False, "FFmpeg 进程启动失败"
        ok, err = await self._wait_ffmpeg_alive(proc)
        if ok:
            self.process = proc
            self.voice_info_used = True
            return True, ""

        # 第一次失败:推流地址多半已失效,刷新后重试
        logger.warning(f"⚠️ ffmpeg 首次启动失败,刷新推流重试: {err}")
        if proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if not await self._refresh_voice_endpoint(bot):
            return False, err
        proc = self._spawn_ffmpeg(song_url, self.voice_info, offset_ms)
        if proc is None:
            return False, "FFmpeg 重试启动失败"
        ok, err = await self._wait_ffmpeg_alive(proc)
        if not ok:
            return False, err
        self.process = proc
        self.voice_info_used = True
        return True, ""

    # ---------- 主流程 ----------
    async def play(self, bot, msg: Optional[Message],
                   song: Dict) -> Tuple[bool, str]:
        """播放一首歌:由选歌后、monitor 自动连播、skip 调用。"""
        if not self.current_channel_id:
            return False, "机器人未加入语音频道"

        # URL 解析:song['url'] 优先,否则查 outer/url
        song_url = song.get('url')
        if not song_url and song.get('id'):
            song_url = await music_api.get_play_url(song['id'])
        if not song_url:
            return False, f"《{song.get('name', '?')}》不可播放(灰色或下架)"

        # 终止旧进程 + 旧 monitor
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        if self.process and self.process.poll() is None:
            logger.debug("切歌:终止当前 ffmpeg")
            self._terminate_process()
            await asyncio.sleep(0.3)

        ok, err = await self._start_playback(bot, song_url, offset_ms=0)
        if not ok:
            self.is_playing = False
            return False, err

        self.is_playing = True
        self.is_paused = False
        self._play_started_at = time.monotonic()
        self._paused_offset_ms = 0
        self.monitor_task = asyncio.create_task(self.monitor_playback(bot))
        logger.info(f"🎵 开始播放: {song['name']} - {song.get('artist', '?')}")
        return True, f"正在播放: {song['name']} - {song.get('artist', '?')}"

    async def monitor_playback(self, bot):
        """监控 ffmpeg 退出后自动连播下一首。"""
        try:
            while self.process and self.process.poll() is None:
                await asyncio.sleep(1)
            # 已退出。被外部 stop/pause 主动改了状态就不连播
            if not self.is_playing or self.is_paused:
                return
            exit_code = self.process.returncode if self.process else -1
            logger.info(f"🎵 当前歌曲结束(exit={exit_code})")
            self.current_index += 1
            if self.current_index < len(self.playlist):
                next_song = self.playlist[self.current_index]
                logger.info(f"🎵 自动连播: {next_song['name']}")
                ok, status = await self.play(bot, None, next_song)
                if not ok:
                    logger.warning(f"⚠️ 连播失败,跳过: {status}")
                    # 失败的曲目直接被 current_index 越过,继续监控下一首
                    self.monitor_task = asyncio.create_task(
                        self.monitor_playback(bot))
            else:
                logger.info("🎵 队列已播完")
                self.is_playing = False
        except asyncio.CancelledError:
            logger.debug("🎵 monitor_playback 被取消")
        except Exception as e:
            logger.error(f"❌ 监控异常: {e}")

    # ---------- 控制 ----------
    def stop(self):
        """彻底停止 + 清队列。"""
        self.is_playing = False
        self.is_paused = False
        self.playlist = []
        self.current_index = 0
        self._play_started_at = None
        self._paused_offset_ms = 0
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        self._terminate_process()

    async def pause(self) -> bool:
        """真暂停:杀 ffmpeg + 累计已播 wall-clock。"""
        if not self.is_playing or self.is_paused:
            return False
        if not self.process or self.process.poll() is not None:
            return False
        if self._play_started_at is not None:
            elapsed_ms = int((time.monotonic() - self._play_started_at) * 1000)
            self._paused_offset_ms += elapsed_ms
            self._play_started_at = None
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        self._terminate_process()
        self.is_paused = True
        logger.info(f"🎵 已暂停 @ {self._paused_offset_ms}ms")
        return True

    async def resume(self, bot) -> bool:
        """真继续:刷新推流 + 用 -ss offset 重启 ffmpeg。"""
        if not self.is_paused:
            return False
        if not self.playlist or self.current_index >= len(self.playlist):
            return False
        song = self.playlist[self.current_index]
        song_url = song.get('url')
        if not song_url and song.get('id'):
            song_url = await music_api.get_play_url(song['id'])
        if not song_url:
            return False
        # resume 必然刷新推流(暂停期间地址通常已废)
        if not await self._refresh_voice_endpoint(bot):
            return False
        ok, err = await self._start_playback(
            bot, song_url, offset_ms=self._paused_offset_ms)
        if not ok:
            logger.error(f"❌ resume 失败: {err}")
            return False
        self.is_paused = False
        self.is_playing = True
        self._play_started_at = time.monotonic()
        self.monitor_task = asyncio.create_task(self.monitor_playback(bot))
        logger.info(f"🎵 已继续 @ {self._paused_offset_ms}ms")
        return True

    async def skip(self, bot) -> bool:
        """跳过当前歌:主动播下一首。"""
        if not self.playlist:
            return False
        if self.current_index + 1 >= len(self.playlist):
            return False
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        self._terminate_process()
        self.is_paused = False
        self._paused_offset_ms = 0
        self.current_index += 1
        next_song = self.playlist[self.current_index]
        ok, _ = await self.play(bot, None, next_song)
        return ok

    def get_progress_ms(self) -> int:
        """已播 ms(wall-clock 估算,精度 ~1-2s)。暂停时停在 _paused_offset_ms。"""
        if self.is_paused:
            return self._paused_offset_ms
        if self.is_playing and self._play_started_at is not None:
            elapsed = (time.monotonic() - self._play_started_at) * 1000
            return self._paused_offset_ms + int(elapsed)
        return 0

    def get_status(self) -> str:
        if self.is_paused and self.playlist:
            cur = self.playlist[self.current_index]
            return (f"⏸️ 已暂停: {cur.get('name','?')} - {cur.get('artist','?')}"
                    f" @ {self._paused_offset_ms / 1000:.1f}s")
        if not self.is_playing:
            return "未在播放"
        if self.playlist and self.current_index < len(self.playlist):
            cur = self.playlist[self.current_index]
            return f"▶️ 正在播放: {cur['name']} - {cur.get('artist','?')}"
        return "未知状态"

    def get_queue_text(self) -> str:
        if not self.playlist:
            return "📭 队列为空"
        lines = [f"📋 当前队列({len(self.playlist)} 首):\n"]
        for i, s in enumerate(self.playlist):
            if i == self.current_index and self.is_playing:
                mark = "▶️"
            elif i == self.current_index and self.is_paused:
                mark = "⏸️"
            elif i < self.current_index:
                mark = "✅"
            else:
                mark = "  "
            lines.append(f"{mark} {i+1}. {s['name']} - {s.get('artist','?')}")
        return "\n".join(lines)

    def clear_queue(self):
        """清队列,保留当前正在播/暂停的那一首。"""
        if (self.is_playing or self.is_paused) and self.playlist:
            cur = self.playlist[self.current_index]
            self.playlist = [cur]
            self.current_index = 0
        else:
            self.playlist = []
            self.current_index = 0


def _fmt_duration_ms(ms: int) -> str:
    """ms → 'M:SS' 或 'H:MM:SS'。无效输入返回 '?'。"""
    if not ms or ms <= 0:
        return "?"
    s = int(ms / 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m}:{sec:02d}"


def build_music_card(player: "MusicPlayer") -> list:
    """生成音乐播放器主卡片(KOOK 卡片 JSON)。

    空闲态:只有 [🎵 加歌] / [📋 队列] 两个按钮。
    播放态:展示当前曲目 + 进度 + 队列窗口 + 6 个控制按钮。
    """
    def _btn(text, value, theme="primary"):
        return {"type": "button", "theme": theme, "click": "return-val",
                "value": value, "text": {"type": "plain-text", "content": text}}

    modules = [{
        "type": "header",
        "text": {"type": "plain-text", "content": "🎵 音乐播放器"},
    }]

    if not player.is_playing and not player.is_paused:
        # 空闲态
        modules.append({
            "type": "section",
            "text": {"type": "kmarkdown",
                     "content": "📭 **当前没在播放**\n\n点 [🎵 加歌] 开始点歌喵~"},
        })
        modules.append({"type": "action-group", "elements": [
            _btn("🎵 加歌", "music:add", "primary"),
            _btn("🔄 刷新", "music:refresh", "info"),
        ]})
        return [{"type": "card", "theme": "secondary", "size": "lg",
                 "modules": modules}]

    # 播放/暂停态:显示当前曲目 + 进度
    cur = player.playlist[player.current_index] if player.playlist else {}
    name = cur.get("name", "?")
    artist = cur.get("artist", "?")
    duration_ms = cur.get("duration", 0)
    progress_ms = player.get_progress_ms()
    state_icon = "⏸️" if player.is_paused else "▶️"
    state_text = "已暂停" if player.is_paused else "正在播放"
    progress_str = _fmt_duration_ms(progress_ms)
    duration_str = _fmt_duration_ms(duration_ms)

    modules.append({
        "type": "section",
        "text": {"type": "kmarkdown",
                 "content": (f"{state_icon} **{state_text}**\n\n"
                             f"《{name}》 - {artist}\n"
                             f"⏱️ {progress_str} / {duration_str}")},
    })

    # 队列窗口:当前 ± 几首,过长用省略提示
    if player.playlist:
        total = len(player.playlist)
        # 显示当前前 1 后 5,最多 7 行
        start = max(0, player.current_index - 1)
        end = min(total, player.current_index + 6)
        lines = [f"📋 **队列(共 {total} 首):**"]
        if start > 0:
            lines.append(f"  ⋯ ({start} 首已播)")
        for i in range(start, end):
            s = player.playlist[i]
            if i == player.current_index and player.is_playing:
                mark = "▶️"
            elif i == player.current_index and player.is_paused:
                mark = "⏸️"
            elif i < player.current_index:
                mark = "✅"
            else:
                mark = "▫️"
            lines.append(f"{mark} {i+1}. {s['name']} - {s.get('artist','?')}")
        if end < total:
            lines.append(f"  ⋯ (还有 {total - end} 首)")
        modules.append({"type": "divider"})
        modules.append({
            "type": "section",
            "text": {"type": "kmarkdown", "content": "\n".join(lines)},
        })

    # 控制按钮:暂停/继续动态切换
    play_pause_btn = (_btn("▶️ 继续", "music:resume", "success")
                      if player.is_paused
                      else _btn("⏸️ 暂停", "music:pause", "primary"))
    modules.append({"type": "divider"})
    modules.append({"type": "action-group", "elements": [
        play_pause_btn,
        _btn("⏭️ 下一首", "music:next", "info"),
        _btn("⏹️ 停止", "music:stop", "danger"),
    ]})
    modules.append({"type": "action-group", "elements": [
        _btn("🎵 加歌", "music:add", "primary"),
        _btn("🗑️ 清空", "music:clear", "warning"),
        _btn("🔄 刷新", "music:refresh", "info"),
    ]})

    return [{"type": "card", "theme": "secondary", "size": "lg",
             "modules": modules}]


class MusicAPI:
    """网易云音乐 API。"""
    def __init__(self):
        self.search_url = "https://music.163.com/api/cloudsearch/pc"
        self.play_url_base = "https://music.163.com"
        self._headers = {
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/120.0.0.0 Safari/537.36'),
            'Referer': 'https://music.163.com/',
            'Accept': '*/*',
            'Accept-Language': 'zh-CN,zh;q=0.9',
        }

    async def search(self, keyword: str, limit: int = 5) -> List[Dict]:
        try:
            params = {'s': keyword, 'type': 1, 'limit': limit, 'offset': 0}
            logger.info(f"🎵 搜索: {keyword}")
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.search_url, params=params,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    text = await resp.text()
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                logger.warning(f"⚠️ 搜索返回非 JSON: {text[:200]}")
                return []

            songs = []
            for item in result.get('result', {}).get('songs', []):
                if item.get('fee', 0) == 1:  # 付费歌跳过
                    continue
                ar = item.get('artists') or item.get('ar') or []
                artists = ', '.join(a.get('name', '') for a in ar) or '未知'
                songs.append({
                    'id': item.get('id'),
                    'name': item.get('name'),
                    'artist': artists,
                    'album': ((item.get('album') or {}).get('name', '')
                              if item.get('album') else ''),
                    'duration': item.get('duration', 0),
                })
            logger.info(f"✅ 搜索完成,返回 {len(songs)} 首")
            return songs
        except Exception as e:
            logger.error(f"❌ 搜索失败: {e}")
            return []

    async def get_play_url(self, song_id: int) -> Optional[str]:
        """获取歌曲实际 URL,过滤灰色/下架/默认提示音。"""
        url = f"{self.play_url_base}/song/media/outer/url?id={song_id}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, allow_redirects=True,
                    headers=self._headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    final_url = str(resp.url)
                    content_length = resp.content_length
                    # 灰色检测:跳到 404 / id=0 默认页 / 内容过小(默认提示音)
                    if 'music.163.com/404' in final_url:
                        logger.warning(f"⚠️ 歌曲 {song_id} 已下架")
                        return None
                    if final_url.endswith('id=0') or final_url.rstrip('/').endswith('id=0'):
                        logger.warning(f"⚠️ 歌曲 {song_id} 不可播放")
                        return None
                    if content_length is not None and content_length < 5000:
                        logger.warning(
                            f"⚠️ 歌曲 {song_id} 内容仅 {content_length}B,可能是默认提示音")
                        return None
                    logger.debug(f"✅ 播放链接: {final_url[:60]}...")
                    return final_url
        except Exception as e:
            logger.error(f"❌ 获取播放链接失败: {e}")
            return None


music_api = MusicAPI()


# ---------- 公共入口 ----------
def _ensure_player() -> MusicPlayer:
    global music_player
    if music_player is None:
        music_player = MusicPlayer()
    return music_player


def is_in_music_selection(user_id: str) -> bool:
    return user_id in music_selections


def set_music_player_info(voice_info: dict, channel_id: str, guild_id: str):
    """由 bot.py 在 /进频道 成功后调用,把推流信息预存,首次 /听歌 可复用。"""
    player = _ensure_player()
    player.voice_info = voice_info
    player.voice_info_used = False
    player.current_channel_id = channel_id
    player.current_guild_id = guild_id
    logger.debug(f"已预存推流信息 channel={channel_id}")


async def handle_music_command(msg: Message, bot):
    """处理 /听歌 命令:进入搜索流程。"""
    user_id = msg.author_id
    if type(msg).__name__ == 'PrivateMessage':
        await msg.reply("❌ 请在服务器频道使用此命令~")
        return
    try:
        guild_id = msg.guild.id
    except Exception:
        guild_id = None
    if not guild_id:
        await msg.reply("❌ 无法获取服务器信息")
        return

    try:
        result = await bot.client.gate.exec_req(khl_api.Voice.list())
        if not result.get('items'):
            await msg.reply("❌ 我不在语音频道,先发 `进频道` 让我加入~")
            return
    except Exception as e:
        logger.error(f"❌ 查语音频道失败: {e}")
        await msg.reply("❌ 无法获取语音频道状态,先 `进频道`~")
        return

    _ensure_player()
    music_selections[user_id] = {'guild_id': guild_id, 'step': 'waiting_keyword'}
    await msg.reply("🎵 请输入要搜索的歌曲名或歌手名~")


def _parse_choice(content: str, max_n: int) -> Optional[List[int]]:
    """解析多选输入:'1' / '1,3,5' / '1，3' / 'all'。返回 0-based 索引列表。

    无效输入返回 None;有效但全越界返回 None。
    """
    s = content.strip().lower().replace(' ', '')
    if not s:
        return None
    if s == 'all':
        return list(range(max_n))
    s = s.replace('，', ',')  # 全角逗号 → 半角
    try:
        if ',' in s:
            parts = [int(x) for x in s.split(',') if x]
        else:
            parts = [int(s)]
    except ValueError:
        return None
    out = []
    for n in parts:
        if 1 <= n <= max_n and (n - 1) not in out:
            out.append(n - 1)
    return out or None


async def handle_music_input(msg: Message, bot) -> bool:
    """处理 music_selections 状态中的输入。返回 True 表示已处理。"""
    user_id = msg.author_id
    content = msg.content.strip()
    if user_id not in music_selections:
        return False

    player = _ensure_player()
    selection = music_selections[user_id]
    step = selection.get('step')

    if step == 'waiting_keyword':
        songs = await music_api.search(content, limit=5)
        if not songs:
            await msg.reply("❌ 没找到,换个关键词?")
            return True
        selection['songs'] = songs
        selection['step'] = 'waiting_choice'
        lines = ["🔍 搜索结果:\n"]
        for i, s in enumerate(songs, 1):
            d = s.get('duration', 0) // 1000
            lines.append(f"{i}. {s['name']} - {s['artist']} "
                         f"({d//60:02d}:{d%60:02d})")
        lines.append("\n回复编号选择(支持 `1,3,5` 多选 / `all` 全部加入队列)")
        await msg.reply("\n".join(lines))
        return True

    if step == 'waiting_choice':
        songs = selection.get('songs', [])
        idxs = _parse_choice(content, len(songs))
        if idxs is None:
            await msg.reply("❌ 输入无效,例:`1` 或 `1,3` 或 `all`")
            return True
        chosen = [{
            'id': songs[i]['id'],
            'name': songs[i]['name'],
            'artist': songs[i]['artist'],
            'album': songs[i].get('album', ''),
            'url': f"https://music.163.com/song/media/outer/url?id={songs[i]['id']}",
        } for i in idxs]

        # 已在播 → 追加到队尾;空闲 → 覆盖并立即播放第一首
        if player.is_playing or player.is_paused:
            player.playlist.extend(chosen)
            await msg.reply(
                f"➕ 已加入队列 {len(chosen)} 首,当前队列共 {len(player.playlist)} 首")
        else:
            player.playlist = list(chosen)
            player.current_index = 0
            player.current_guild_id = selection.get('guild_id')
            ok, status = await player.play(bot, msg, chosen[0])
            if ok:
                extra = f"(队列共 {len(chosen)} 首)" if len(chosen) > 1 else ""
                await msg.reply(f"🎵 {status} {extra}".strip())
            else:
                await msg.reply(f"❌ {status}")
        del music_selections[user_id]
        return True

    return False


async def handle_music_control(msg: Message, bot, content: str) -> bool:
    """处理音乐控制命令。返回 True 表示已处理(消息分发要 return)。"""
    player = _ensure_player()
    c = content.strip().lower()

    if c in ('music_stop', '/music_stop', '停止', 'stop'):
        player.stop()
        if player.current_channel_id:
            await player.leave_channel(bot)
        await msg.reply("⏹️ 已停止并离开频道")
        return True

    if c in ('music_pause', '/music_pause', '暂停', 'pause'):
        if await player.pause():
            await msg.reply("⏸️ 已暂停(用 `继续` 接着播)")
        else:
            await msg.reply("❌ 当前没有在播放")
        return True

    if c in ('music_resume', '/music_resume', '继续', 'resume'):
        if await player.resume(bot):
            cur = (player.playlist[player.current_index]
                   if player.playlist else {})
            await msg.reply(f"▶️ 已继续: {cur.get('name', '?')}")
        else:
            await msg.reply("❌ 没有可继续的播放")
        return True

    if c in ('music_skip', '/music_skip', '下一首', 'skip', '切歌'):
        if await player.skip(bot):
            await msg.reply("⏭️ 已切到下一首")
        else:
            await msg.reply("❌ 队列里没有下一首了")
        return True

    if c in ('music_status', '/music_status', '播放状态', 'status'):
        await msg.reply(f"🎵 {player.get_status()}")
        return True

    if c in ('queue', '队列', '播放列表'):
        await msg.reply(player.get_queue_text())
        return True

    if c in ('clear', '清空队列', '清空'):
        player.clear_queue()
        await msg.reply("🗑️ 队列已清空(保留正在播的)")
        return True

    return False
