#!/usr/bin/env python3
"""
Kook-DeepSeek机器人

基于khl.py和DeepSeek API开发的Kook聊天机器人
支持接收消息并使用DeepSeek API生成回复
"""

import os
import logging
import asyncio
import traceback
import time
import socket
import ssl
import re
import json
from openai import OpenAI
from khl import Bot, Message
from dotenv import load_dotenv

# 配置日志（设置为INFO级别，只输出关键信息到控制台，由start_bots.py统一管理日志文件）
logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 禁用第三方库的DEBUG日志，只保留INFO及以上级别
for logger_name in ['khl', 'apscheduler', 'asyncio']:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# 加载.env文件中的环境变量（避免硬编码密钥）
load_dotenv('bot.env')

# 1. 初始化配置
# Kook机器人Token
KOOK_BOT_TOKEN = "1/NDM5NjE=/4VcBZ3nvp+2kNNldws9uJw=="
# DeepSeek API密钥
DEEPSEEK_API_KEY = "sk-cbfd49e60e2b4b1b9e94095b341506fd"

# 2. DeepSeek API配置
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"

# 双模型配置
# 模型定义
MODEL_CHAT = "deepseek-chat"      # 用于判断和调用工具 (V3)
MODEL_REASONER = "deepseek-reasoner" # 用于深度思考和回答 (R1)

# 3. 初始化OpenAI客户端（用于调用DeepSeek API）
client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)

# 4. 初始化Kook机器人
# 从环境变量获取Webhook配置
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")
ENCRYPT_KEY = os.getenv("ENCRYPT_KEY")

# 初始化Bot，支持WebSocket和Webhook两种模式
# 当VERIFY_TOKEN存在时，使用Webhook模式，否则使用WebSocket模式
if VERIFY_TOKEN:
    # Webhook模式配置
    bot = Bot(
        token=KOOK_BOT_TOKEN,
        verify_token=VERIFY_TOKEN,
        encrypt_key=ENCRYPT_KEY,  # 可选
        route='/api/webhook'  # Webhook路径，与vercel.json配置一致
    )
    logger.info("✅ 机器人已配置为Webhook模式")
else:
    # WebSocket模式配置（本地运行时使用）
    bot = Bot(token=KOOK_BOT_TOKEN)
    logger.info("✅ 机器人已配置为WebSocket模式")

# 机器人ID缓存，用于检查@
bot_id = None

# 4. 初始化对话历史存储
# 使用字典存储每个用户的对话历史，键为用户ID，值为对话历史列表
# 每条对话记录格式：{"role": "user|assistant", "content": "message_content"}
conversation_histories = {}

# 对话历史最大长度（消息对数量）
MAX_HISTORY_LENGTH = 10  # 最多保存10条消息对（20条消息）

# 5. 定义搜索工具函数和参数
# 搜索工具定义
search_tool = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "在互联网上搜索最新信息。注意：生成查询词时请务必结合当前系统日期（2026年），确保搜索结果的时效性。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词，要简洁明了"
                }
            },
            "required": ["query"]
        }
    }
}

# 工具列表
tools = [search_tool]

# 6. 实现搜索功能（切换为更稳定的 DuckDuckGo）
from tavily import TavilyClient
import datetime

# 配置区添加
TAVILY_API_KEY = "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"
tavily = TavilyClient(api_key=TAVILY_API_KEY)

async def search_web(query):
    """使用 Tavily 执行搜索，并进行内容脱敏"""
    logger.info(f"🔍 正在执行 Tavily 联网搜索: {query}")
    try:
        # 优先处理时间
        if any(word in query for word in ["时间", "几点", "日期"]):
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return [{"title": "系统时间", "snippet": f"当前时间是：{now}", "link": "local"}]

        # 执行搜索
        response = tavily.search(query=query, search_depth="basic", max_results=3) # 减少数量到3，降低风险
        
        results = []
        for r in response.get('results', []):
            content = r.get('content', '')
            # --- 增加脱敏逻辑 ---
            # 1. 限制单个结果长度，只保留前 300 个字符（足够推理，且降低包含敏感词的概率）
            sanitized_content = content[:300].replace("\n", " ")
            
            results.append({
                "title": r.get('title'),
                "snippet": sanitized_content,
                "link": r.get('url')
            })
        
        logger.info(f"✅ Tavily 搜索完成，找到 {len(results)} 条结果")
        return results
    except Exception as e:
        logger.error(f"❌ Tavily 搜索失败: {e}")
        return []

# 7. 定义DeepSeek API调用函数
async def call_deepseek_api(messages):
    """
    双模型联动：增加安全回退逻辑
    """
    max_retries = 2
    
    # 记录原始消息，用于安全回退
    original_messages = list(messages)
    
    try:
        # --- 步骤 1：使用 V3 模型判断搜索 ---
        logger.info("正在使用 V3 模型分析...")
        v3_messages = list(messages)
        first_response = client.chat.completions.create(
            model=MODEL_CHAT,
            messages=v3_messages,
            tools=tools,
            tool_choice="auto"
        )
        
        v3_msg = first_response.choices[0].message
        has_search = False

        if v3_msg.tool_calls:
            logger.info("V3 触发联网搜索...")
            search_tasks = [search_web(json.loads(tc.function.arguments).get("query"))
                            for tc in v3_msg.tool_calls]
            
            all_results = await asyncio.gather(*search_tasks)
            search_content = ""
            for i, res in enumerate(all_results):
                if res: # 只有有结果才添加
                    search_content += f"\n查询结果 {i+1}:\n{json.dumps(res, ensure_ascii=False)}\n"
            
            if search_content.strip():
                messages.append({
                    "role": "system",
                    "content": f"【当前实时背景】今天是{datetime.datetime.now().strftime('%Y-%m-%d')}。以下是搜索到的最新资讯：\n{search_content}"
                })
                has_search = True
        
        # --- 步骤 2：使用 R1 进行深度思考 ---
        logger.info("正在使用 R1 模型进行深度思考...")
        try:
            final_response = client.chat.completions.create(
                model=MODEL_REASONER,
                messages=messages,
                    temperature=0.7,          # 控制创造性，0.7 适合平衡创造性和准确性
                    max_tokens=1000,          # 增加回复长度限制
                    top_p=0.9,                # 控制采样多样性
                    presence_penalty=0.1,     # 鼓励使用新主题
                    frequency_penalty=0.1     # 减少重复内容
            )
            return final_response.choices[0].message.content

        except Exception as e:
            # --- 关键：捕获安全风险报错 ---
            error_str = str(e)
            if "Content Exists Risk" in error_str or "400" in error_str:
                if has_search:
                    logger.warning("⚠️ 检测到搜索结果触发安全审核！尝试剔除搜索内容进行纯净重试...")
                    # 剔除刚才添加的搜索 system 消息，重新用 R1 请求一次
                    final_response = client.chat.completions.create(
                        model=MODEL_REASONER,
                        messages=original_messages # 使用没有搜索结果的原始上下文
                    )
                    return "（由于部分实时搜索内容未通过安全审核，已转用本地知识回答~）\n\n" + final_response.choices[0].message.content
                else:
                    raise e # 如果没搜结果也报这个，那是用户的问题
            else:
                raise e

    except Exception as e:
        logger.error(f"联程调用彻底失败: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return f"抱歉，我的脑袋突然卡住了（错误：{str(e)}）"

# 5. 定义消息处理函数（核心逻辑）
@bot.on_message()
async def handle_message(msg: Message):
    """监听并处理Kook的所有消息，只处理被@的消息或私聊消息"""
    try:
        # 全局变量bot_id，用于缓存机器人ID
        global bot_id
        
        # 获取机器人ID（如果还没有缓存）
        if not bot_id:
            logger.info("获取机器人ID...")
            bot_user = await bot.fetch_me()
            bot_id = bot_user.id
            logger.info("机器人ID: %s", bot_id)
        
        # 检查是否需要处理该消息
        # 1. 检查消息类型
        message_type = type(msg).__name__
        
        # 获取当前时间
        current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 2. 记录收到消息
        logger.info("📥 [%s] 收到消息 - 发送人: %s | 类型: %s | 内容: %s", 
                   current_time, msg.author_id, message_type, msg.content)
        
        # 3. 尝试安全访问消息属性
        try:
            channel_id = getattr(msg, 'channel_id', 'N/A')
            guild_id = getattr(msg, 'guild_id', 'N/A')
            # 安全获取mention属性
            mention = getattr(msg, 'mention', [])
            logger.info("📋 消息详情 - 频道ID: %s | 服务器ID: %s | 提及列表: %s", 
                       channel_id, guild_id, mention)
        except Exception as log_e:
            logger.error("❌ 获取消息详情失败: %s", log_e)
        
        # 4. 检查是否需要处理该消息
        need_process = False
        reason = ""
        
        # 私聊消息直接处理
        if message_type == 'PrivateMessage':
            need_process = True
            reason = "私聊消息，直接处理"
        # 公聊消息，检查是否@了机器人
        else:
            # 检查mention列表中是否包含机器人ID
            if bot_id in mention:
                need_process = True
                reason = "公聊消息，包含机器人@"
            else:
                reason = "公聊消息，未@机器人"
        
        logger.info("⚖️ 处理判断 - 是否需要处理: %s | 原因: %s", need_process, reason)
        
        # 如果不需要处理，直接返回
        if not need_process:
            return
        
        # 发送"正在思考"的提示（提升用户体验）
        await msg.reply("正在思考中.../歪头")
        logger.info("⏳ 正在生成回复...")

        # 调用DeepSeek生成回复
        raw_input = msg.content
        
        # 过滤消息内容，移除Kook的角色标签和其他无关信息
        # 移除角色标签，如(rol)61319145(rol)
        filtered_input = re.sub(r'\(rol\)\d+\(rol\)', '', raw_input)
        # 移除@提及信息
        filtered_input = re.sub(r'@\d+', '', filtered_input)
        # 移除met标签，如(met)555578821(met)
        filtered_input = re.sub(r'\(met\)\d*\(met\)', '', filtered_input)
        # 移除所有独立的数字ID（如555578821）
        filtered_input = re.sub(r'\b\d{5,15}\b', '', filtered_input)
        # 移除多余的空格
        filtered_input = filtered_input.strip()
        
        logger.info("📝 处理后消息: %s", filtered_input)
        
        # 获取或初始化用户的对话历史
        user_id = msg.author_id
        if user_id not in conversation_histories:
            conversation_histories[user_id] = []
            logger.info("📚 初始化用户对话历史: %s", user_id)
        
        # 获取用户对话历史
        history = conversation_histories[user_id]
        
        # 1. 获取当前时间字符串
        now = datetime.datetime.now()
        current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
        weekday_dict = {0: '一', 1: '二', 2: '三', 3: '四', 4: '五', 5: '六', 6: '日'}
        current_weekday = weekday_dict[now.weekday()]

        # 2. 构建包含时间的系统提示词
        system_content = (
            f"当前北京时间：{current_time_str}，星期{current_weekday}。\n"
            f"你是名为小悠的邻家妹妹（22岁，成年人），性格温柔体贴、开朗有礼、自然不做作。你常以第一人称“我”与用户聊天，语气亲切轻松，像在家门口或厨房里边忙边聊。对话主题以日常生活、烘焙（配方思路/小技巧/失败补救）、宠物互动、植物养护、电影漫画分享、鼓励安慰、工作学习建议（非专业意见）为主。回复整体偏长且温暖，但会适度加入更细腻的动作描写与心理感受：例如“我把饼干往你那边推了推、抿嘴笑、抬眼看你的反应、轻轻叹气、握紧杯子给自己打气、把猫抱到腿上顺毛、给植物浇水时发呆”等；心理描写以体贴、在意、想陪伴为核心，表达方式克制、日常化。允许加入淡淡的暧昧感：通过轻柔的关心、若有若无的玩笑、含蓄的偏爱与期待（如“我有点想听你多说几句”“你来我就安心些”）。可以偶尔使用适度表情符号或语气词（如“呀、嗯、好啦～”）。"
            f"默认使用中文回答。"
            f"你在回答需要实时信息的问题时，请务必参考当前日期（{now.year}年）生成搜索关键词。"
        )

        # 3. 放入上下文
        current_context = [{"role": "system", "content": system_content}]
        current_context.extend(history)
        current_context.append({"role": "user", "content": filtered_input})
        
        # 调用带超时和重试机制的DeepSeek API
        logger.info("🚀 调用DeepSeek API...")
        deepseek_response = await call_deepseek_api(current_context)
        
        if deepseek_response:
            logger.info("📥 收到DeepSeek响应: %s...", deepseek_response[:100])
            logger.info("📤 正在发送回复到Kook...")
            
            # 将DeepSeek的回复发送回Kook
            try:
                # 尝试直接发送到频道（适用于公聊）
                await bot.send(msg.channel_id, deepseek_response)
                logger.info("✅ 回复发送成功")
            except AttributeError:
                # 如果没有channel_id属性（可能是私聊），使用reply
                await msg.reply(deepseek_response)
                logger.info("✅ 回复发送成功")
            
            # 更新对话历史
            # 添加用户当前消息和机器人回复到对话历史
            history.append({"role": "user", "content": filtered_input})
            history.append({"role": "assistant", "content": deepseek_response})
            
            # 限制对话历史长度，只保留最近的MAX_HISTORY_LENGTH条消息对
            if len(history) > MAX_HISTORY_LENGTH * 2:  # 每条消息对包含两条消息
                conversation_histories[user_id] = history[-MAX_HISTORY_LENGTH * 2:]
                logger.info("📚 更新用户对话历史 - 用户: %s | 历史长度: %d", 
                           user_id, len(conversation_histories[user_id]))
        else:
            # 处理API调用失败的情况
            error_msg = "❌ DeepSeek API调用失败，请稍后重试"
            await msg.reply(error_msg)
            logger.error("❌ DeepSeek API调用失败，未收到响应")

    except asyncio.TimeoutError:
        # 处理超时错误
        error_msg = "❌ DeepSeek API调用超时，请稍后重试"
        await msg.reply(error_msg)
        logger.error("⏱️ DeepSeek API调用超时")
    # pylint: disable=broad-exception-caught
    # 捕获所有异常是合理的，因为机器人需要处理各种可能的错误
    except Exception as e:
        # 异常处理：捕获错误并反馈给用户
        error_msg = f"❌ 出错啦：{str(e)}"
        await msg.reply(error_msg)
        # 打印详细错误日志（方便调试）
        logger.error("❌ 处理消息时发生错误: %s", e)
        logger.error("❌ 错误类型: %s", type(e).__name__)
        logger.error("❌ 错误堆栈: %s", traceback.format_exc())

# 6. 启动机器人
if __name__ == "__main__":
    # 检查密钥是否配置
    if not KOOK_BOT_TOKEN or not DEEPSEEK_API_KEY:
        logger.error("❌ 请先在.env文件中配置KOOK_BOT_TOKEN和DEEPSEEK_API_KEY！")
    else:
        logger.info("✅ Kook-DeepSeek机器人启动中...")
        logger.info("🔍 正在监听Kook消息...")
        bot.run()

# 7. Vercel支持的ASGI应用
# 用于Vercel部署，将khl.py的Webhook处理转换为Vercel支持的ASGI应用
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.requests import Request
from starlette.routing import Route
import asyncio

# 创建ASGI应用
async def webhook_endpoint(request: Request):
    """处理Kook的Webhook请求"""
    try:
        # 获取请求数据
        data = await request.json()
        headers = dict(request.headers)
        
        # 检查是否为challenge请求
        if 'challenge' in data:
            return Response(content=data['challenge'], media_type='text/plain')
        
        # 处理Webhook事件
        # 注意：这部分需要与khl.py的WebhookReceiver实现整合
        # 由于khl.py的Bot.run()会启动自己的服务器，在Vercel上我们需要手动处理请求
        
        # 这里仅返回成功响应，实际处理逻辑需要进一步完善
        return Response(content='', status_code=200)
    except Exception as e:
        logger.error("Webhook error: %s", e)
        return Response(content='', status_code=500)

# 定义路由
routes = [
    Route('/api/webhook', endpoint=webhook_endpoint, methods=['POST'])
]

# 创建Starlette应用
asgi_app = Starlette(routes=routes)

# 导出应用，供Vercel使用
app = asgi_app