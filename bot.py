#!/usr/bin/env python3
"""
Kook-DeepSeek 机器人

基于 khl.py 与 DeepSeek API 的 KOOK 聊天机器人,
支持 AI 对话、联网搜索(Tavily)、签到、用户语音频道、网易云音乐播放。
"""
import asyncio
import datetime
import json
import logging
import os
import random
import re
import sys
import traceback

from dotenv import load_dotenv
from openai import OpenAI
from tavily import TavilyClient

import khl.api as khl_api
from khl import Bot, Message

from kook_music import (
    handle_music_command,
    handle_music_control,
    handle_music_input,
    is_in_music_selection,
    set_music_player_info,
)

# ---------- 日志 ----------
logging.basicConfig(level=logging.INFO, format="%(message)s",
                    handlers=[logging.StreamHandler()])
logger = logging.getLogger(__name__)
for name in ("khl", "apscheduler", "asyncio"):
    logging.getLogger(name).setLevel(logging.WARNING)

# ---------- 环境变量 ----------
load_dotenv("bot.env")
KOOK_BOT_TOKEN = os.getenv("KOOK_BOT_TOKEN")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")

_missing = [k for k, v in {
    "KOOK_BOT_TOKEN": KOOK_BOT_TOKEN,
    "DEEPSEEK_API_KEY": DEEPSEEK_API_KEY,
    "TAVILY_API_KEY": TAVILY_API_KEY,
}.items() if not v]
if _missing:
    logger.error("❌ 缺少环境变量: %s,请在 bot.env 中配置", ", ".join(_missing))
    sys.exit(1)

# ---------- 客户端 ----------
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
tavily = TavilyClient(api_key=TAVILY_API_KEY)
bot = Bot(token=KOOK_BOT_TOKEN)
logger.info("✅ 机器人已配置为 WebSocket 模式")

# ---------- 常量 ----------
MODEL_NAME = "deepseek-v4-pro"        # 对话模型
REASONING_EFFORT = "high"             # 思考强度:high / max
MAX_HISTORY_LENGTH = 10               # 每用户保留的对话轮数
MAX_TOOL_ROUNDS = 1                   # 单次对话允许的工具调用轮数(超出后强制收尾)

DATA_DIR = "data"
CHECKIN_FILE = os.path.join(DATA_DIR, "checkin_data.json")
USER_DB_FILE = os.path.join(DATA_DIR, "user_database.json")
os.makedirs(DATA_DIR, exist_ok=True)

MIN_SCORE, MAX_SCORE = 3, 10

# ---------- 状态 ----------
bot_id = None
conversation_histories: dict = {}
voice_selections: dict = {}
checkin_data: dict = {}
user_database: dict = {}


def _load_json(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("❌ 加载 %s 失败: %s", path, e)
        return {}


def _save_json(path: str, data: dict) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error("❌ 保存 %s 失败: %s", path, e)


checkin_data = _load_json(CHECKIN_FILE)
user_database = _load_json(USER_DB_FILE)
logger.info("✅ 加载数据:签到 %d 条,用户库 %d 条", len(checkin_data), len(user_database))


# ---------- 搜索工具 ----------
search_tool = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "在互联网上搜索最新信息。生成查询词时请结合当前年份以保证时效性。",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "搜索关键词,简洁明了"}},
            "required": ["query"],
        },
    },
}
tools = [search_tool]


_PERCENT_ENC_RE = re.compile(r"(%[0-9A-Fa-f]{2}){2,}")
_WHITESPACE_RE = re.compile(r"\s+")


def _sanitize_text(text: str, limit: int) -> str:
    """裁剪文本,移除 percent-encoded 段(常从 URL 残留),折叠空白。"""
    if not text:
        return ""
    text = _PERCENT_ENC_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:limit]


def _sanitize_url(url: str, limit: int = 80) -> str:
    """URL 脱敏:把 percent-encoded 段折叠为占位符,再截断。"""
    if not url:
        return ""
    return _PERCENT_ENC_RE.sub("...", url)[:limit]


async def search_web(query: str):
    """使用 Tavily 执行搜索并对结果做脱敏处理,降低 DeepSeek 安全审核误判风险。"""
    logger.info("🔍 Tavily 搜索: %s", query)
    try:
        response = tavily.search(query=query, search_depth="basic", max_results=3)
        results = [{
            "title": _sanitize_text(r.get("title"), 100),
            "snippet": _sanitize_text(r.get("content"), 200),
            "link": _sanitize_url(r.get("url")),
        } for r in response.get("results", [])]
        logger.info("✅ 搜索完成,共 %d 条", len(results))
        return results
    except Exception as e:
        logger.error("❌ Tavily 搜索失败: %s", e)
        return []


# ---------- 用户信息 / 签到 ----------
async def fetch_user_info(user_id: str):
    """从 KOOK API 获取用户信息并写入本地数据库。"""
    try:
        user = await bot.fetch_user(user_id)
        info = {
            "id": user.id,
            "username": getattr(user, "username", ""),
            "nickname": getattr(user, "nickname", ""),
            "identify_num": getattr(user, "identify_num", ""),
            "avatar": getattr(user, "avatar", ""),
            "is_vip": getattr(user, "is_vip", False),
            "bot": getattr(user, "bot", False),
            "status": getattr(user, "status", 0),
            "os": getattr(user, "os", ""),
            "online": getattr(user, "online", False),
            "roles": getattr(user, "roles", []),
            "joined_at": getattr(user, "joined_at", 0),
            "active_time": getattr(user, "active_time", 0),
        }
        user_database[user_id] = info
        _save_json(USER_DB_FILE, user_database)
        logger.info("✅ 已缓存用户信息: %s - %s", user_id, info["username"])
        return info
    except Exception as e:
        logger.error("❌ 获取用户信息失败: %s", e)
        return None


async def handle_checkin(msg: Message):
    user_id = msg.author_id
    today = datetime.datetime.now().strftime("%Y-%m-%d")

    if user_id not in user_database:
        await fetch_user_info(user_id)

    record = checkin_data.setdefault(user_id, {
        "last_checkin_date": "", "total_days": 0, "total_score": 0,
    })

    if record["last_checkin_date"] == today:
        await msg.reply(
            f"🐱 你今日已经签过到啦,不要贪心哦~\n"
            f"累计签到:{record['total_days']}天\n当前积分:{record['total_score']}分"
        )
        return

    score = random.randint(MIN_SCORE, MAX_SCORE)
    record["total_days"] += 1
    record["total_score"] += score
    record["last_checkin_date"] = today
    _save_json(CHECKIN_FILE, checkin_data)

    logger.info("📅 用户 %s 签到 +%d 积分,累计 %d 天", user_id, score, record["total_days"])
    await msg.reply(
        f"🎉 签到成功!\n获得积分:{score}分\n"
        f"累计签到:{record['total_days']}天\n当前积分:{record['total_score']}分\n\n"
        f"🎊 继续保持签到好习惯哦~"
    )


async def handle_checkin_list(msg: Message):
    if not checkin_data:
        await msg.reply("🐱 还没有用户签到呢,快来做第一个签到的人吧~")
        return

    sorted_users = sorted(checkin_data.items(),
                          key=lambda x: x[1]["total_days"], reverse=True)[:10]
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = ["🏆 **签到排行榜** 🏆\n", f"📅 统计时间:{now}\n"]
    for i, (uid, data) in enumerate(sorted_users, 1):
        name = uid
        u = user_database.get(uid)
        if u:
            name = u.get("nickname") or u.get("username") or uid
            if u.get("identify_num"):
                name = f"{name}#{u['identify_num']}"
        lines.append(f"{i}. 用户:{name}\n   累计签到:{data['total_days']}天\n"
                     f"   当前积分:{data['total_score']}分\n")
    lines.append(f"目前已有 {len(sorted_users)} 位用户参与签到,继续加油哦~\n🎊 争取上榜吧!")
    await msg.reply("\n".join(lines))


# ---------- 语音频道 ----------
async def join_voice_channel_local(channel_id: str, guild_id: str = None) -> dict:
    result = await bot.client.gate.exec_req(khl_api.Voice.join(channel_id=channel_id))
    if guild_id and result:
        set_music_player_info(result, channel_id, guild_id)
    return result


async def leave_voice_channel_local(channel_id: str):
    await bot.client.gate.exec_req(khl_api.Voice.leave(channel_id=channel_id))


async def list_voice_channels_local() -> list:
    result = await bot.client.gate.exec_req(khl_api.Voice.list())
    return result.get("items", [])


def _require_guild(msg: Message):
    """确认消息来自服务器,返回 guild_id 或 None。"""
    if type(msg).__name__ == "PrivateMessage":
        return None
    try:
        return msg.guild.id
    except Exception:
        return None


async def handle_join_voice(msg: Message):
    guild_id = _require_guild(msg)
    if not guild_id:
        await msg.reply("❌ 请在服务器频道中使用此命令")
        return

    user_id = msg.author_id
    try:
        result = await bot.client.gate.exec_req(
            khl_api.Channel.list(guild_id=guild_id, type=2))
        voice_channels = [
            {"id": item.get("id"), "name": item.get("name", "未知频道")}
            for item in result.get("items", [])
        ]

        if not voice_channels:
            await msg.reply("❌ 当前服务器没有可用的语音频道")
            return

        voice_selections[user_id] = {"guild_id": guild_id, "channels": voice_channels}
        text = "🎤 请选择要进入的语音频道:\n\n"
        text += "\n".join(f"{i}. {vc['name']}" for i, vc in enumerate(voice_channels, 1))
        text += "\n\n请回复数字编号(如:1)"
        await msg.reply(text)
    except Exception as e:
        logger.error("❌ 获取频道列表失败: %s", e)
        await msg.reply(f"❌ 获取频道列表失败:{e}")


async def handle_leave_voice(msg: Message):
    guild_id = _require_guild(msg)
    if not guild_id:
        await msg.reply("❌ 请在服务器频道中使用此命令")
        return

    try:
        voice_channels = await list_voice_channels_local()
        if not voice_channels:
            await msg.reply("😿 我现在不在任何语音频道中哦")
            return
        for vc in voice_channels:
            cid = vc.get("id")
            if cid:
                await leave_voice_channel_local(cid)
                await msg.reply(f"👋 已离开 {vc.get('name', '语音频道')},下次再见啦~")
                return
        await msg.reply("😿 我现在不在任何语音频道中哦")
    except Exception as e:
        logger.error("❌ 离开语音频道失败: %s", e)
        await msg.reply(f"❌ 离开语音频道失败啦:{e}")


# ---------- DeepSeek 调用 ----------
async def _execute_tool(tool_call) -> str:
    """根据 tool_call 执行对应工具,返回 JSON 字符串。"""
    name = tool_call.function.name
    if name == "search_web":
        query = json.loads(tool_call.function.arguments).get("query", "")
        result = await search_web(query)
        return json.dumps(result, ensure_ascii=False)
    return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)


async def call_deepseek_api(messages: list) -> str:
    """ReAct 循环:工具判定阶段关闭思考,最终回答阶段开启思考。带安全审核回退。"""
    messages = list(messages)
    original_messages = list(messages)
    used_tools = False

    def _final_kwargs(msgs):
        """最终生成阶段的调用参数:开启思考 + high effort。"""
        return {
            "model": MODEL_NAME,
            "messages": msgs,
            "reasoning_effort": REASONING_EFFORT,
            "extra_body": {"thinking": {"type": "enabled"}},
        }

    def _is_audit_err(e):
        s = str(e)
        return "Content Exists Risk" in s or "400" in s

    def _slim_tool_msgs(msgs):
        """把 tool 消息内容裁为仅 title + link(去掉最易触发审核的 snippet 正文)。"""
        out = []
        for m in msgs:
            if m.get("role") == "tool":
                try:
                    items = json.loads(m["content"])
                    if isinstance(items, list):
                        items = [{"title": x.get("title", ""), "link": x.get("link", "")}
                                 for x in items if isinstance(x, dict)]
                        m = {**m, "content": json.dumps(items, ensure_ascii=False)}
                except Exception:
                    pass
            out.append(m)
        return out

    def _safety_retry():
        """分级降级:精简 tool 消息 → 剔除 tool 消息 + 防幻觉 system。"""
        try:
            logger.warning("⚠️ 工具结果触发审核,降级为标题+链接重试")
            r = client.chat.completions.create(**_final_kwargs(_slim_tool_msgs(messages)))
            return r.choices[0].message.content
        except Exception as e:
            if not _is_audit_err(e):
                raise

        logger.warning("⚠️ 降级后仍被拒,完全剔除工具结果并加防幻觉提示")
        safety_msgs = list(original_messages) + [{
            "role": "system",
            "content": (
                "注意:刚刚已为用户联网搜索,但搜索结果被安全过滤拦截了。"
                "请如实告知用户'最新信息暂时不可用',然后基于训练知识简短作答。"
                "不要否认你具备联网搜索的能力,不要说自己'没有实时联网功能'。"
            ),
        }]
        r = client.chat.completions.create(**_final_kwargs(safety_msgs))
        return ("(由于部分实时搜索内容未通过安全审核,已转用本地知识回答喵~)\n\n"
                + r.choices[0].message.content)

    try:
        for round_idx in range(MAX_TOOL_ROUNDS + 1):
            if round_idx < MAX_TOOL_ROUNDS:
                # 判定阶段:不思考,允许工具调用
                kwargs = {
                    "model": MODEL_NAME,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "extra_body": {"thinking": {"type": "disabled"}},
                }
            else:
                # 最终回答阶段:开启思考
                kwargs = _final_kwargs(messages)

            try:
                response = client.chat.completions.create(**kwargs)
            except Exception as e:
                if used_tools and _is_audit_err(e):
                    return _safety_retry()
                raise

            msg = response.choices[0].message
            if not msg.tool_calls:
                return msg.content

            used_tools = True
            logger.info("模型请求工具调用 (%d 个)", len(msg.tool_calls))

            assistant_msg = {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": tc.type,
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            }
            # 工具调用场景下,reasoning_content(若有)需在后续轮次回传
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning:
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)

            results = await asyncio.gather(*[_execute_tool(tc) for tc in msg.tool_calls])
            for tc, result in zip(msg.tool_calls, results):
                messages.append({
                    "role": "tool", "tool_call_id": tc.id, "content": result,
                })

        return msg.content or "喵... 想了好几轮还是没头绪~"

    except Exception as e:
        logger.error("DeepSeek 调用失败: %s\n%s", e, traceback.format_exc())
        return f"喵... 脑袋突然卡住了(错误:{e})"


# ---------- 工具函数 ----------
_FILTER_PATTERNS = (
    re.compile(r"\(rol\)\d+\(rol\)"),
    re.compile(r"@\d+"),
    re.compile(r"\(met\)\d*\(met\)"),
    re.compile(r"\b\d{5,15}\b"),
)


def _strip_kook_tags(text: str) -> str:
    """移除 KOOK 角色/提及标签和长数字 ID。"""
    for p in _FILTER_PATTERNS:
        text = p.sub("", text)
    return text.strip()


# ---------- 主消息分发 ----------
@bot.on_message()
async def handle_message(msg: Message):
    try:
        global bot_id
        if not bot_id:
            bot_user = await bot.fetch_me()
            bot_id = bot_user.id
            logger.info("机器人 ID: %s", bot_id)

        message_type = type(msg).__name__
        user_id = msg.author_id
        mention = getattr(msg, "mention", []) or []
        logger.info("📥 收到消息 [%s] %s: %s", message_type, user_id, msg.content)

        content = _strip_kook_tags(msg.content).lower()

        # 签到指令
        if content in ("qd", "/qd", "签到"):
            await handle_checkin(msg)
            return
        if content in ("qdlist", "/qdlist"):
            await handle_checkin_list(msg)
            return

        # 语音频道选择中
        if user_id in voice_selections:
            data = voice_selections[user_id]
            channels = data.get("channels", [])
            try:
                idx = int(content)
                if 1 <= idx <= len(channels):
                    selected = channels[idx - 1]
                    await join_voice_channel_local(selected["id"], data.get("guild_id"))
                    await msg.reply(f"🎉 成功进入 {selected['name']} 啦!")
                else:
                    await msg.reply("❌ 输入无效,请回复有效的数字编号")
            except ValueError:
                await msg.reply("❌ 请输入数字编号(如:1)")
            except Exception as e:
                logger.error("❌ 加入语音频道失败: %s", e)
                await msg.reply(f"❌ 进入语音频道失败啦:{e}")
            voice_selections.pop(user_id, None)
            return

        # 语音频道指令
        if content in ("join", "/join", "进频道", "来"):
            await handle_join_voice(msg)
            return
        if content in ("leave", "/leave", "离开", "退频道"):
            await handle_leave_voice(msg)
            return

        # 音乐指令
        if content in ("music", "/music", "音乐", "听歌"):
            await handle_music_command(msg, bot, khl_api)
            return
        if await handle_music_control(msg, bot, khl_api, content):
            return
        if is_in_music_selection(user_id) and await handle_music_input(msg, bot, khl_api):
            return

        # AI 对话:私聊直接处理,公聊需 @
        if message_type != "PrivateMessage" and bot_id not in mention:
            return

        await msg.reply("🐱正在思考中...")

        filtered_input = _strip_kook_tags(msg.content)
        history = conversation_histories.setdefault(user_id, [])

        now = datetime.datetime.now()
        weekday = "一二三四五六日"[now.weekday()]
        system_prompt = (
            f"当前北京时间:{now.strftime('%Y-%m-%d %H:%M:%S')},星期{weekday}。\n"
            f"你是一位 'AI 猫娘' (Catgirl) 角色的系统助手。"
            f"始终以温柔、活泼、带一点撒娇但不失礼貌的口吻与用户互动;"
            f"默认使用中文回答。说话时可适度添加猫咪化的语尾(如 '喵')。"
            f"在回答需要实时信息的问题时,请务必参考当前日期({now.year}年)生成搜索关键词。"
        )

        context = [{"role": "system", "content": system_prompt}]
        context.extend(history)
        context.append({"role": "user", "content": filtered_input})

        response = await call_deepseek_api(context)
        if not response:
            await msg.reply("❌ DeepSeek API 调用失败,请稍后重试")
            return

        try:
            await bot.send(msg.channel_id, response)
        except AttributeError:
            await msg.reply(response)

        history.append({"role": "user", "content": filtered_input})
        history.append({"role": "assistant", "content": response})
        if len(history) > MAX_HISTORY_LENGTH * 2:
            conversation_histories[user_id] = history[-MAX_HISTORY_LENGTH * 2:]

    except asyncio.TimeoutError:
        await msg.reply("❌ DeepSeek API 调用超时,请稍后重试")
        logger.error("⏱️ DeepSeek API 调用超时")
    except Exception as e:
        logger.error("❌ 处理消息异常: %s\n%s", e, traceback.format_exc())
        await msg.reply(f"❌ 出错啦:{e}")


# ---------- 启动 ----------
if __name__ == "__main__":
    logger.info("✅ Kook-DeepSeek 机器人启动中...")
    bot.run()
