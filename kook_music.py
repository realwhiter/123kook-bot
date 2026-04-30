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
from khl import Message, MessageTypes

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
            '-acodec', 'libopus', '-ab', '128k',
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
        """监控 ffmpeg 退出后自动连播下一首。

        wall-clock 兜底:如果实际播放进度已经超过 duration+10s 但 ffmpeg
        还活着(典型场景:推流端点被挤掉,ffmpeg 在向死端推 UDP 包没反馈),
        主动 terminate 让"歌曲结束→连播"分支接管。
        """
        try:
            while self.process and self.process.poll() is None:
                await asyncio.sleep(1)
                # 兜底超时检查
                song = (self.playlist[self.current_index]
                        if self.playlist
                        and self.current_index < len(self.playlist)
                        else None)
                duration = song.get('duration', 0) if song else 0
                if duration > 0:
                    progress = self.get_progress_ms()
                    if progress > duration + 10000:
                        logger.warning(
                            "⚠️ 已播 %.1fs 超过歌长 %.1fs,主动终止 ffmpeg 切下一首",
                            progress / 1000, duration / 1000)
                        self._terminate_process()
                        break
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
        """真暂停:杀 ffmpeg + 累计已播 wall-clock。

        cap _paused_offset_ms 到 duration-5s,防止 wall-clock 失控时(monitor
        没及时切歌、ffmpeg 在向死端推流等)offset 超过歌曲长度,导致 resume
        时 ffmpeg `-ss` 大于音频末尾,报 "Output file is empty"。
        """
        if not self.is_playing or self.is_paused:
            return False
        if not self.process or self.process.poll() is not None:
            return False
        if self._play_started_at is not None:
            elapsed_ms = int((time.monotonic() - self._play_started_at) * 1000)
            self._paused_offset_ms += elapsed_ms
            self._play_started_at = None
        # cap 到 duration-5s,5s 安全 buffer 给 ffmpeg seek 用
        song = self.playlist[self.current_index] if self.playlist else None
        duration = song.get('duration', 0) if song else 0
        if duration > 0:
            max_offset = max(0, duration - 5000)
            if self._paused_offset_ms > max_offset:
                logger.warning(
                    "⚠️ 暂停时 wall-clock(%dms)超过歌长(%dms),cap 到 %dms",
                    self._paused_offset_ms, duration, max_offset)
                self._paused_offset_ms = max_offset
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        self._terminate_process()
        self.is_paused = True
        logger.info(f"🎵 已暂停 @ {self._paused_offset_ms}ms")
        return True

    async def resume(self, bot) -> bool:
        """真继续:刷新推流 + 用 -ss offset 重启 ffmpeg。

        offset 接近歌尾时直接切下一首,避免 ffmpeg `-ss 超过音频末尾`。
        失败时把状态彻底清空(否则 is_paused 一直 True,反复点继续会循环失败)。
        """
        if not self.is_paused:
            return False
        if not self.playlist or self.current_index >= len(self.playlist):
            self._reset_after_failure()
            return False
        song = self.playlist[self.current_index]

        # 暂停点离歌尾不到 2s → 这首相当于已经播完,直接切下一首
        duration = song.get('duration', 0)
        if duration > 0 and self._paused_offset_ms >= max(0, duration - 2000):
            logger.info("🎵 暂停点已到歌尾,直接切下一首")
            self.is_paused = False
            self._paused_offset_ms = 0
            if self.current_index + 1 < len(self.playlist):
                self.current_index += 1
                ok, _ = await self.play(bot, None,
                                        self.playlist[self.current_index])
                return ok
            self._reset_after_failure()
            return False

        song_url = song.get('url')
        if not song_url and song.get('id'):
            song_url = await music_api.get_play_url(song['id'])
        if not song_url:
            self._reset_after_failure()
            return False
        if not await self._refresh_voice_endpoint(bot):
            self._reset_after_failure()
            return False
        ok, err = await self._start_playback(
            bot, song_url, offset_ms=self._paused_offset_ms)
        if not ok:
            logger.error(f"❌ resume 失败: {err}")
            self._reset_after_failure()
            return False
        self.is_paused = False
        self.is_playing = True
        self._play_started_at = time.monotonic()
        self.monitor_task = asyncio.create_task(self.monitor_playback(bot))
        logger.info(f"🎵 已继续 @ {self._paused_offset_ms}ms")
        return True

    def _reset_after_failure(self):
        """resume / 切歌失败时把状态恢复到"无播放",避免按钮死循环。"""
        self.is_paused = False
        self.is_playing = False
        self._play_started_at = None
        self._paused_offset_ms = 0
        if self.monitor_task:
            self.monitor_task.cancel()
            self.monitor_task = None
        self._terminate_process()

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

    async def search(self, keyword: str, limit: int = 5,
                     offset: int = 0) -> Tuple[List[Dict], int]:
        """搜索歌曲。返回 (songs, total_count)。

        付费歌(fee=1)在客户端过滤,所以 len(songs) 可能小于 limit;
        total 是网易云返回的总命中数,翻页用它判断是否还有更多。
        """
        try:
            params = {'s': keyword, 'type': 1,
                      'limit': limit, 'offset': offset}
            logger.info(f"🎵 搜索: {keyword} (offset={offset})")
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
                return [], 0

            data = result.get('result', {}) or {}
            total = int(data.get('songCount', 0))
            songs = []
            for item in data.get('songs', []):
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
            logger.info(f"✅ 搜索完成,返回 {len(songs)} 首(总命中 {total})")
            return songs, total
        except Exception as e:
            logger.error(f"❌ 搜索失败: {e}")
            return [], 0

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
        items = result.get('items', [])
        if not items:
            await msg.reply("❌ 我不在语音频道,先发 `进频道` 让我加入~")
            return
    except Exception as e:
        logger.error(f"❌ 查语音频道失败: {e}")
        await msg.reply("❌ 无法获取语音频道状态,先 `进频道`~")
        return

    # bot 重启后 player 单例的 current_channel_id 会丢,从 KOOK 实际状态恢复
    _sync_player_with_voice_list(items, guild_id)
    music_selections[user_id] = {'guild_id': guild_id, 'step': 'waiting_keyword'}
    await msg.reply("🎵 请输入要搜索的歌曲名或歌手名~")


def _sync_player_with_voice_list(voice_items: list, guild_id: str):
    """从 KOOK Voice.list 返回值同步 player 频道状态。

    bot 进程重启后 music_player 是新单例,current_channel_id=None,但 KOOK
    那边 bot 可能还在语音频道(WebSocket 重连前)。这种情况下 player 不知道
    自己"还在哪",后续 play() 会以为没加入语音频道而失败。

    这里在每次 Voice.list 检查通过后调用一次,把 KOOK 当前频道同步到 player。
    voice_info 强制设 None,下次 _ensure_voice_endpoint 会自动 refresh 拿新地址。
    """
    if not voice_items:
        return
    player = _ensure_player()
    if player.current_channel_id:
        return  # 已经有,不覆盖
    cid = voice_items[0].get('id')
    if not cid:
        return
    player.current_channel_id = cid
    player.current_guild_id = guild_id
    player.voice_info = None
    player.voice_info_used = False
    logger.info(f"🎵 从 Voice.list 恢复 player 频道: {cid}")


# ---------- 搜索卡片 ----------
SEARCH_PAGE_SIZE = 5


def build_search_card(selection: dict) -> list:
    """生成搜索结果卡片。selection 必须含 keyword / songs_cache / page / total。"""
    keyword = selection.get('keyword', '')
    page = selection.get('page', 0)
    cache = selection.get('songs_cache', [])
    total = selection.get('total', len(cache))

    start = page * SEARCH_PAGE_SIZE
    end = min(start + SEARCH_PAGE_SIZE, len(cache))
    page_songs = cache[start:end]

    # 还能不能往后翻:cache 里有没消费完的,或网易云那边还有没拿的
    has_next = (end < len(cache)) or (len(cache) < total)
    has_prev = page > 0

    def _btn(text, value, theme="primary"):
        return {"type": "button", "theme": theme, "click": "return-val",
                "value": value,
                "text": {"type": "plain-text", "content": text}}

    modules = [{
        "type": "header",
        "text": {"type": "plain-text", "content": f"🔍 搜索:{keyword}"},
    }]

    if not page_songs:
        modules.append({
            "type": "section",
            "text": {"type": "kmarkdown", "content": "📭 这一页没有结果"},
        })
    else:
        lines = []
        for i, s in enumerate(page_songs):
            d = (s.get('duration', 0) or 0) // 1000
            lines.append(f"`{start+i+1}`. **{s['name']}** - {s['artist']} "
                         f"({d//60:02d}:{d%60:02d})")
        modules.append({
            "type": "section",
            "text": {"type": "kmarkdown", "content": "\n".join(lines)},
        })

    modules.append({
        "type": "context",
        "elements": [{
            "type": "kmarkdown",
            "content": (f"第 {page+1} 页 · 共 {total or '?'} 首结果"
                        f" · 直接发新关键词可重新搜索"),
        }],
    })
    modules.append({"type": "divider"})

    # 选号按钮(每页最多 5 个)。KOOK action-group elements ≤ 4,
    # 5 个按钮拆成 3+2 两组放在两行。
    pick_btns = [_btn(str(i + 1), f"search:pick:{i}", "primary")
                 for i in range(len(page_songs))]
    if pick_btns:
        modules.append({"type": "action-group",
                        "elements": pick_btns[:3]})
        if len(pick_btns) > 3:
            modules.append({"type": "action-group",
                            "elements": pick_btns[3:]})

    # 操作按钮:全部入队 + 翻页 + 关闭
    action_btns = [_btn("➕ 全部入队", "search:all", "success")]
    if has_prev:
        action_btns.append(_btn("⏪ 上页", "search:prev", "info"))
    if has_next:
        action_btns.append(_btn("⏩ 下页", "search:next", "info"))
    action_btns.append(_btn("❌ 关闭", "search:close", "danger"))
    modules.append({"type": "action-group", "elements": action_btns})

    return [{"type": "card", "theme": "secondary", "size": "lg",
             "modules": modules}]


async def _send_or_update_search_card(bot, user_id: str, target_id: str):
    """发送或就地更新某用户的搜索卡片(策略 B:update 失败回退新发)。"""
    if user_id not in music_selections:
        return
    selection = music_selections[user_id]
    content = json.dumps(build_search_card(selection), ensure_ascii=False)

    msg_id = selection.get('card_msg_id')
    if msg_id and selection.get('card_target_id') == target_id:
        try:
            await bot.client.gate.exec_req(khl_api.Message.update(
                msg_id=msg_id, content=content))
            return
        except Exception as e:
            logger.warning(f"⚠️ 更新搜索卡片失败,改为重发: {e}")
            selection['card_msg_id'] = None

    try:
        result = await bot.client.gate.exec_req(khl_api.Message.create(
            type=MessageTypes.CARD.value,
            target_id=target_id, content=content))
        new_msg_id = (result or {}).get('msg_id')
        if new_msg_id:
            selection['card_msg_id'] = new_msg_id
            selection['card_target_id'] = target_id
        else:
            logger.warning(f"⚠️ Message.create 未返回 msg_id: {result}")
    except Exception as e:
        logger.error(f"❌ 发送搜索卡片失败: {e}")


async def _delete_search_card(bot, user_id: str):
    """删除搜索卡 + 清掉 selection 状态。失败忽略(消息已不存在等)。"""
    if user_id not in music_selections:
        return
    selection = music_selections[user_id]
    msg_id = selection.get('card_msg_id')
    if msg_id:
        try:
            await bot.client.gate.exec_req(
                khl_api.Message.delete(msg_id=msg_id))
        except Exception as e:
            logger.debug(f"删除搜索卡失败(忽略): {e}")
    del music_selections[user_id]


async def _enqueue_or_play(bot, songs: List[Dict], guild_id: str
                           ) -> Tuple[bool, str]:
    """把 chosen 歌入队或立即播放。返回 (ok, feedback_text)。"""
    player = _ensure_player()
    if player.is_playing or player.is_paused:
        player.playlist.extend(songs)
        return True, (f"➕ 已加入队列 {len(songs)} 首,"
                      f"当前队列共 {len(player.playlist)} 首")
    player.playlist = list(songs)
    player.current_index = 0
    player.current_guild_id = guild_id
    ok, status = await player.play(bot, None, songs[0])
    if not ok:
        return False, f"❌ {status}"
    extra = f"(队列共 {len(songs)} 首)" if len(songs) > 1 else ""
    return True, f"🎵 {status} {extra}".strip()


async def handle_search_card_button(bot, value: str, user_id: str,
                                    channel_id: str) -> dict:
    """处理搜索卡 search:* 按钮。返回 {feedback?, refresh_music_card?}。

    feedback:要发给点击者的文字(空字符串则不发)
    refresh_music_card:True 则上层应刷新音乐卡(歌已入队/播放)
    """
    if user_id not in music_selections:
        return {"feedback": "❌ 这张搜索卡已过期,请重新发起搜索"}

    selection = music_selections[user_id]
    if selection.get('step') != 'card_active':
        return {"feedback": "❌ 这张搜索卡已过期"}

    cache = selection.get('songs_cache', [])
    page = selection.get('page', 0)
    page_size = SEARCH_PAGE_SIZE
    start = page * page_size
    end = min(start + page_size, len(cache))

    if value.startswith('search:pick:'):
        try:
            idx_in_page = int(value.split(':')[2])
        except (ValueError, IndexError):
            return {"feedback": "❌ 选项无效"}
        global_idx = start + idx_in_page
        if global_idx >= len(cache):
            return {"feedback": "❌ 选项无效"}
        chosen = [_song_to_play_dict(cache[global_idx])]
        ok, fb = await _enqueue_or_play(bot, chosen, selection['guild_id'])
        await _delete_search_card(bot, user_id)
        return {"feedback": fb, "refresh_music_card": ok}

    if value == 'search:all':
        page_songs = cache[start:end]
        if not page_songs:
            return {"feedback": "❌ 这一页没有歌"}
        chosen = [_song_to_play_dict(s) for s in page_songs]
        ok, fb = await _enqueue_or_play(bot, chosen, selection['guild_id'])
        await _delete_search_card(bot, user_id)
        return {"feedback": fb, "refresh_music_card": ok}

    if value == 'search:next':
        next_page = page + 1
        # cache 不够覆盖下一页时,从网易云补一批
        if (next_page + 1) * page_size > len(cache):
            offset = len(cache)
            new_songs, total = await music_api.search(
                selection['keyword'], limit=page_size, offset=offset)
            cache.extend(new_songs)
            selection['songs_cache'] = cache
            selection['total'] = total
        if next_page * page_size >= len(cache):
            return {"feedback": "已到最后一页"}
        selection['page'] = next_page
        await _send_or_update_search_card(bot, user_id, channel_id)
        return {}

    if value == 'search:prev':
        if page <= 0:
            return {"feedback": "已是第一页"}
        selection['page'] = page - 1
        await _send_or_update_search_card(bot, user_id, channel_id)
        return {}

    if value == 'search:close':
        await _delete_search_card(bot, user_id)
        return {"feedback": "🗑️ 已关闭搜索"}

    return {"feedback": f"❌ 未知按钮: {value}"}


def _song_to_play_dict(s: Dict) -> Dict:
    """把 search 出来的 song 项展开成播放器可消费的格式。"""
    return {
        'id': s['id'],
        'name': s['name'],
        'artist': s['artist'],
        'album': s.get('album', ''),
        'url': f"https://music.163.com/song/media/outer/url?id={s['id']}",
    }


async def handle_music_input(msg: Message, bot) -> bool:
    """处理 music_selections 状态中的文字输入。返回 True 表示已处理。

    waiting_keyword:首次输入歌名 → 搜索 → 发卡片 → 切到 card_active
    card_active:发新关键词 → 重置到第 1 页重新搜索 + 更新原卡
    """
    user_id = msg.author_id
    content = msg.content.strip()
    if user_id not in music_selections:
        return False

    selection = music_selections[user_id]
    step = selection.get('step')

    if step in ('waiting_keyword', 'card_active'):
        if not content:
            await msg.reply("❌ 关键词不能为空")
            return True
        songs, total = await music_api.search(
            content, limit=SEARCH_PAGE_SIZE, offset=0)
        if not songs:
            await msg.reply("❌ 没找到,换个关键词?")
            return True
        selection['keyword'] = content
        selection['songs_cache'] = list(songs)
        selection['total'] = total
        selection['page'] = 0
        selection['step'] = 'card_active'
        await _send_or_update_search_card(bot, user_id, msg.target_id)
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
