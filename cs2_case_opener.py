import json
import random
from typing import List, Dict, Any, Optional
from khl import Message, Event

# 稀有度概率映射（普通武器箱）
RARITY_PROBABILITIES = {
    "普通级": 0.0,
    "军规级": 0.7992,
    "受限": 0.1598,      # 与数据文件中的"受限"匹配
    "保密": 0.0320,       # 与数据文件中的"保密"匹配
    "隐秘": 0.0064,       # 与数据文件中的"隐秘"匹配
    "非凡": 0.0026        # 金色物品（刀/手套）
}

# 用户状态类
class UserState:
    def __init__(self):
        self.current_opener = CS2CaseOpener()
        self.selected_collection = None
        self.waiting_for_search = False  # 标记用户是否正在等待搜索关键词

# 全局用户状态字典
user_states = {}

# 获取或创建用户状态
def get_user_state(user_id: str) -> UserState:
    if user_id not in user_states:
        user_states[user_id] = UserState()
    return user_states[user_id]

class CS2CaseOpener:
    def __init__(self):
        self.collections = self._load_collections()
        self.current_page = 1
        self.items_per_page = 5
        self.search_keyword = ""
    
    def _load_collections(self) -> List[Dict[str, Any]]:
        """加载收藏品数据"""
        try:
            with open("csqaq_collections.json", "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            print("错误：未找到csqaq_collections.json文件")
            return []
        except json.JSONDecodeError:
            print("错误：csqaq_collections.json文件格式错误")
            return []
    
    def set_search_keyword(self, keyword: str) -> None:
        """设置搜索关键词"""
        self.search_keyword = keyword
        self.current_page = 1  # 搜索时重置页码
    
    def get_filtered_collections(self) -> List[Dict[str, Any]]:
        """获取过滤后的收藏品列表"""
        if not self.search_keyword:
            return self.collections
        
        filtered = []
        for collection in self.collections:
            if self.search_keyword in collection["name"]:
                filtered.append(collection)
        return filtered
    
    def get_page_collections(self) -> List[Dict[str, Any]]:
        """获取当前页的收藏品"""
        filtered = self.get_filtered_collections()
        start = (self.current_page - 1) * self.items_per_page
        end = start + self.items_per_page
        return filtered[start:end]
    
    def next_page(self) -> bool:
        """下一页"""
        filtered = self.get_filtered_collections()
        total_pages = (len(filtered) + self.items_per_page - 1) // self.items_per_page
        if self.current_page < total_pages:
            self.current_page += 1
            return True
        return False
    
    def prev_page(self) -> bool:
        """上一页"""
        if self.current_page > 1:
            self.current_page -= 1
            return True
        return False
    
    def get_collection_by_name(self, name: str) -> Dict[str, Any] or None:
        """根据名称获取收藏品"""
        for collection in self.collections:
            if collection["name"] == name:
                return collection
        return None
    
    def calculate_rarity_weights(self, collection_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """计算物品的稀有度权重"""
        # 首先按稀有度分组
        rarity_groups = {}
        for item in collection_items:
            rarity = item["rln"]
            if rarity not in rarity_groups:
                rarity_groups[rarity] = []
            rarity_groups[rarity].append(item)
        
        # 计算每组的权重
        weighted_items = []
        
        # 先确定每个稀有度的目标概率
        target_probabilities = {}
        for rarity in RARITY_PROBABILITIES:
            if RARITY_PROBABILITIES[rarity] > 0:
                target_probabilities[rarity] = RARITY_PROBABILITIES[rarity]
        
        # 检查是否有对应的物品
        available_rarities = [rarity for rarity in target_probabilities if rarity in rarity_groups]
        
        if not available_rarities:
            # 如果没有可用的稀有度，返回所有物品，不考虑概率
            for items in rarity_groups.values():
                for item in items:
                    weighted_items.append({
                        "item": item,
                        "weight": 1.0
                    })
            return weighted_items
        
        # 计算总概率
        total_target_prob = sum(target_probabilities[rarity] for rarity in available_rarities)
        
        # 为每个可用稀有度计算实际概率（按目标概率比例分配）
        actual_probabilities = {}
        for rarity in available_rarities:
            actual_probabilities[rarity] = target_probabilities[rarity] / total_target_prob
        
        # 为每个物品分配权重
        for rarity in available_rarities:
            items = rarity_groups[rarity]
            probability = actual_probabilities[rarity]
            
            # 为该稀有度的每个物品分配相同的基础权重
            item_weight = probability / len(items)
            for item in items:
                weighted_items.append({
                    "item": item,
                    "weight": item_weight
                })
        
        return weighted_items
    
    def open_case(self, collection: Dict[str, Any]) -> List[Dict[str, Any]]:
        """模拟开箱，生成10个随机物品"""
        collection_items = collection["containers"]
        
        # 计算权重
        weighted_items = self.calculate_rarity_weights(collection_items)
        
        # 如果没有可开箱的物品，返回空列表
        if not weighted_items:
            return []
        
        # 总权重
        total_weight = sum(item["weight"] for item in weighted_items)
        
        # 生成10个物品
        results = []
        for _ in range(10):
            # 加权随机选择
            rand = random.uniform(0, total_weight)
            current = 0
            selected_item = None
            
            for weighted_item in weighted_items:
                current += weighted_item["weight"]
                if rand <= current:
                    selected_item = weighted_item["item"]
                    break
            
            if selected_item:
                results.append(selected_item)
        
        return results
    
    def _get_random_wear(self) -> str:
        """获取随机磨损值和磨损等级"""
        # 磨损等级及对应的范围
        wear_levels = [
            ("崭新出厂", 0.0, 0.07),
            ("略有磨损", 0.07, 0.15),
            ("久经沙场", 0.15, 0.38),
            ("破损不堪", 0.38, 0.45),
            ("战痕累累", 0.45, 0.80)
        ]
        
        # 随机选择一个磨损等级
        wear_level = random.choice(wear_levels)
        wear_name, min_wear, max_wear = wear_level
        
        # 在该等级范围内生成随机磨损值
        wear_value = round(random.uniform(min_wear, max_wear), 3)
        
        return f"{wear_value} ({wear_name})"
    
    def format_opening_result(self, collection_name: str, results: List[Dict[str, Any]]) -> str:
        """格式化开箱结果，使用卡片消息格式"""
        if not results:
            return f"> 🎁 {collection_name} 开箱结果 🎁\n> (spl)抱歉，{collection_name}开箱失败，无法获取物品列表(spl)"
        
        # 生成卡片消息内容
        card_content = [
            {
                "type": "card",
                "theme": "secondary",
                "size": "lg",
                "modules": [
                    # 标题模块
                    {
                        "type": "header",
                        "text": {
                            "type": "plain-text",
                            "content": f"🎁 {collection_name} 开箱结果"
                        }
                    },
                    # 分割线
                    {
                        "type": "divider"
                    },
                    # 说明文字
                    {
                        "type": "section",
                        "text": {
                            "type": "plain-text",
                            "content": "点击下方剧透查看开箱结果："
                        }
                    }
                ]
            }
        ]
        
        # 生成开箱结果模块
        # 为了避免模块过多，将结果分成多个section
        current_section = []
        for i, item in enumerate(results, 1):
            rarity = item["rln"]
            short_name = item["short_name"]
            
            # 获取随机磨损值
            wear = self._get_random_wear()
            
            # 获取稀有度对应的颜色主题
            rarity_color = self._get_rarity_color(rarity)
            
            # 使用font标签格式化稀有度，仅在卡片中可用
            item_line = f"{i}. {short_name} (font){rarity}(font)[{rarity_color}] {wear}"
            
            # 添加到当前section
            current_section.append(item_line)
            
            # 每5个物品一个section，或者最后一组
            if len(current_section) == 5 or i == len(results):
                card_content[0]["modules"].append({
                    "type": "section",
                    "text": {
                        "type": "kmarkdown",
                        "content": "\n".join([f"(spl){line}(spl)" for line in current_section])
                    }
                })
                current_section = []
        
        # 添加分割线
        card_content[0]["modules"].append({
            "type": "divider"
        })
        
        # 添加再次开箱按钮
        card_content[0]["modules"].append({
            "type": "action-group",
            "elements": [
                {
                    "type": "button",
                    "theme": "primary",
                    "value": f"open {collection_name}",
                    "click": "return-val",
                    "text": {
                        "type": "plain-text",
                        "content": "再次开箱"
                    }
                }
            ]
        })
        
        # 返回卡片消息的列表对象，khl.py的reply方法可以直接处理
        return card_content
    
    def _get_rarity_color(self, rarity: str) -> str:
        """获取稀有度对应的颜色主题，仅在卡片消息中使用"""
        # 根据官方文档，card中font标签支持的theme有：
        # primary, success, danger, warning, info, secondary, body, tips, pink, purple
        rarity_colors = {
            "普通级": "secondary",  # 灰色
            "军规级": "info",       # 蓝色
            "受限": "purple",      # 紫色
            "受限级": "purple",    # 紫色
            "保密": "success",     # 绿色
            "保密 ": "success",    # 绿色（处理带空格的情况）
            "保密级": "success",   # 绿色
            "隐秘": "danger",      # 红色
            "隐秘级": "danger",    # 红色
            "非凡": "warning",     # 黄色（金色效果）
            "违禁": "danger",      # 红色（违禁物品）
            "大师": "primary",      # 主色（金色/橙色）
            "奇异": "tips",         # 特殊颜色（粉色）
            "工业级": "info",       # 蓝色
            "消费级": "secondary",  # 灰色
            "高级": "purple"        # 紫色
        }
        return rarity_colors.get(rarity, "body")
    
    def _get_rarity_emoji(self, rarity: str) -> str:
        """获取稀有度对应的 emoji"""
        rarity_emojis = {
            "普通级": "⬜",       # 白色
            "军规级": "🔵",       # 蓝色
            "受限": "🟣",       # 紫色
            "受限级": "🟣",     # 紫色
            "保密": "🟢",        # 绿色
            "保密 ": "🟢",      # 绿色（处理带空格的情况）
            "保密级": "🟢",      # 绿色
            "隐秘": "🔴",        # 红色
            "隐秘级": "🔴",      # 红色
            "非凡": "⭐",       # 金色/黄色
            "违禁": "💀",        # 骷髅头（违禁物品）
            "大师": "👑",        # 皇冠
            "奇异": "✨",        # 闪亮
            "工业级": "🔧",       # 工具
            "消费级": "💰",       # 金钱
            "高级": "🎯"         # 目标/高级
        }
        return rarity_emojis.get(rarity, "")
    
    def format_collection_list(self) -> list:
        """格式化收藏品列表，使用卡片消息格式，包含按钮"""
        page_collections = self.get_page_collections()
        filtered = self.get_filtered_collections()
        total_pages = (len(filtered) + self.items_per_page - 1) // self.items_per_page
        
        # 生成卡片消息内容
        card_content = [
            {
                "type": "card",
                "theme": "secondary",
                "size": "lg",
                "modules": [
                    # 标题模块
                    {
                        "type": "header",
                        "text": {
                            "type": "plain-text",
                            "content": "📦 CS2 收藏品列表"
                        }
                    },
                    # 分割线
                    {
                        "type": "divider"
                    }
                ]
            }
        ]
        
        # 搜索关键词显示
        if self.search_keyword:
            card_content[0]["modules"].append({
                "type": "section",
                "text": {
                    "type": "plain-text",
                    "content": f"🔍 搜索关键词：{self.search_keyword}"
                }
            })
        
        # 收藏品列表
        for i, collection in enumerate(page_collections, 1):
            # 为每个收藏品添加一个按钮，用于选择开箱
            card_content[0]["modules"].append({
                "type": "section",
                "text": {
                    "type": "plain-text",
                    "content": f"{i}. {collection['name']}"
                },
                "mode": "right",
                "accessory": {
                    "type": "button",
                    "theme": "primary",
                    "value": f"open {collection['name']}",
                    "click": "return-val",
                    "text": {
                        "type": "plain-text",
                        "content": "开箱"
                    }
                }
            })
        
        # 页码显示
        card_content[0]["modules"].append({
            "type": "section",
            "text": {
                "type": "plain-text",
                "content": f"📄 第 {self.current_page}/{total_pages} 页，共 {len(filtered)} 个收藏品"
            }
        })
        
        # 翻页按钮
        card_content[0]["modules"].append({
            "type": "action-group",
            "elements": [
                {
                    "type": "button",
                    "theme": "secondary",
                    "value": "prev",
                    "click": "return-val",
                    "text": {
                        "type": "plain-text",
                        "content": "上一页"
                    }
                },
                {
                    "type": "button",
                    "theme": "secondary",
                    "value": "next",
                    "click": "return-val",
                    "text": {
                        "type": "plain-text",
                        "content": "下一页"
                    }
                },
                {
                    "type": "button",
                    "theme": "success",
                    "value": "search",
                    "click": "return-val",
                    "text": {
                        "type": "plain-text",
                        "content": "搜索"
                    }
                }
            ]
        })
        
        return card_content

# 事件处理类
def setup_bot_handlers(bot):
    """设置Bot事件处理函数"""
    
    # 命令处理：/cs2
    @bot.command(name='cs2')
    async def cs2_main(msg: Message, *args):
        """CS2开箱模拟器主命令
        用法：
        - /cs2 list - 显示收藏品列表
        - /cs2 search <关键词> - 搜索收藏品
        - /cs2 next - 下一页
        - /cs2 prev - 上一页
        - /cs2 open <收藏品名称> - 开箱
        """
        user_state = get_user_state(msg.author_id)
        current_opener = user_state.current_opener
        
        # 解析参数
        action = args[0] if args else 'list'
        keyword = ' '.join(args[1:]) if len(args) > 1 else ''
        
        if action == 'list':
            # 显示收藏品列表
            response = current_opener.format_collection_list()
            await msg.reply(response)
        
        elif action == 'search':
            # 搜索收藏品
            if not keyword:
                await msg.reply('请输入搜索关键词，例如：/cs2 search 命悬一线')
                return
            
            current_opener.set_search_keyword(keyword)
            response = current_opener.format_collection_list()
            await msg.reply(response)
        
        elif action == 'next':
            # 下一页
            if current_opener.next_page():
                response = current_opener.format_collection_list()
                await msg.reply(response)
            else:
                await msg.reply('已经是最后一页了')
        
        elif action == 'prev':
            # 上一页
            if current_opener.prev_page():
                response = current_opener.format_collection_list()
                await msg.reply(response)
            else:
                await msg.reply('已经是第一页了')
        
        elif action == 'open':
            # 开箱
            if not keyword:
                await msg.reply('请输入收藏品名称，例如：/cs2 open 命悬一线武器箱')
                return
            
            collection = current_opener.get_collection_by_name(keyword)
            if not collection:
                await msg.reply(f'未找到名称为 "{keyword}" 的收藏品')
                return
            
            # 模拟开箱
            results = current_opener.open_case(collection)
            # 格式化结果
            response = current_opener.format_opening_result(collection["name"], results)
            # 直接发送卡片消息JSON字符串，khl.py会自动处理
            await msg.reply(response)
        
        else:
            # 未知命令
            await msg.reply('未知命令，请使用 /cs2 list 查看帮助')
    
    # 命令处理：/open
    @bot.command(name='open')
    async def quick_open(msg: Message, *args):
        """快速开箱命令
        用法：/open <收藏品名称>
        """
        collection_name = ' '.join(args) if args else ''
        
        if not collection_name:
            await msg.reply('请输入收藏品名称，例如：/open 命悬一线武器箱')
            return
        
        user_state = get_user_state(msg.author_id)
        current_opener = user_state.current_opener
        
        collection = current_opener.get_collection_by_name(collection_name)
        if not collection:
            await msg.reply(f'未找到名称为 "{collection_name}" 的收藏品')
            return
        
        # 模拟开箱
        results = current_opener.open_case(collection)
        # 格式化结果
        response = current_opener.format_opening_result(collection["name"], results)
        # 直接发送卡片消息JSON字符串，khl.py会自动处理
        await msg.reply(response)
    
    # 命令处理：/case
    @bot.command(name='case')
    async def case_list(msg: Message, *args):
        """显示收藏品列表（快捷命令）
        用法：/case
        """
        user_state = get_user_state(msg.author_id)
        current_opener = user_state.current_opener
        
        response = current_opener.format_collection_list()
        await msg.reply(response)
    
    # 按钮点击事件处理
    from khl import EventTypes
    
    @bot.on_event(EventTypes.MESSAGE_BTN_CLICK)
    async def handle_button_click(bot, event: Event):
        """处理卡片消息按钮点击事件"""
        try:
            # 打印事件数据，用于调试（只打印关键信息，避免编码问题）
            print(f"按钮点击事件 - 用户ID: {event.body.get('user_id')}, 按钮值: {event.body.get('value')}")
        except Exception as e:
            # 捕获编码错误，避免程序崩溃
            print(f"打印按钮点击事件数据时发生错误: {e}")
        
        # 获取事件数据
        user_id = event.body.get('user_id')
        value = event.body.get('value')
        msg_id = event.body.get('msg_id')
        
        # 从event.body中获取正确的channel_id，不同事件类型可能有不同的字段名
        channel_id = event.body.get('target_id') or event.body.get('channel_id')
        
        if not user_id or not value:
            print(f"缺少必要参数：user_id={user_id}, value={value}, channel_id={channel_id}")
            return
        
        # 如果没有channel_id，尝试从event.extra或其他字段获取
        if not channel_id:
            channel_id = event.extra.get('channel_id') if hasattr(event, 'extra') and event.extra else None
        
        # 获取用户状态
        user_state = get_user_state(user_id)
        current_opener = user_state.current_opener
        
        # 根据按钮值执行不同操作
        try:
            if value == 'prev':
                # 上一页
                if current_opener.prev_page():
                    response = current_opener.format_collection_list()
                    if channel_id:
                        # 使用Bot的client.send方法发送消息，先获取Channel对象
                        channel = await bot.client.fetch_public_channel(channel_id)
                        await bot.client.send(channel, response, type=10)
                    else:
                        print("无法获取通道ID，无法发送消息")
            elif value == 'next':
                # 下一页
                if current_opener.next_page():
                    response = current_opener.format_collection_list()
                    if channel_id:
                        channel = await bot.client.fetch_public_channel(channel_id)
                        await bot.client.send(channel, response, type=10)
                    else:
                        print("无法获取通道ID，无法发送消息")
            elif value == 'search':
                # 搜索
                user_state.waiting_for_search = True
                if channel_id:
                    channel = await bot.client.fetch_public_channel(channel_id)
                    await bot.client.send(channel, '请输入搜索关键词：', type=9)
                else:
                    print("无法获取通道ID，无法发送消息")
            elif value.startswith('open '):
                # 开箱
                collection_name = value[5:]
                collection = current_opener.get_collection_by_name(collection_name)
                if collection:
                    results = current_opener.open_case(collection)
                    response = current_opener.format_opening_result(collection["name"], results)
                    if channel_id:
                        channel = await bot.client.fetch_public_channel(channel_id)
                        await bot.client.send(channel, response, type=10)
                    else:
                        print("无法获取通道ID，无法发送消息")
                else:
                    if channel_id:
                        channel = await bot.client.fetch_public_channel(channel_id)
                        await bot.client.send(channel, f'未找到名称为 "{collection_name}" 的收藏品', type=9)
                    else:
                        print(f"未找到名称为 '{collection_name}' 的收藏品，但无法获取通道ID发送消息")
        except Exception as e:
            print(f"处理按钮点击事件时发生错误：{e}")
            import traceback
            traceback.print_exc()
            if channel_id:
                try:
                    channel = await bot.client.fetch_public_channel(channel_id)
                    await bot.client.send(channel, f'处理按钮点击事件时发生错误：{e}', type=9)
                except Exception as send_error:
                    print(f"发送错误消息时发生错误：{send_error}")
    
    # 处理用户消息，用于接收搜索关键词
    @bot.on_message()
    async def handle_case_message(msg: Message):
        """处理用户消息，用于接收搜索关键词和@机器人指令"""
        # 获取用户状态
        user_state = get_user_state(msg.author_id)
        
        # 如果用户正在等待搜索关键词
        if user_state.waiting_for_search:
            user_state.waiting_for_search = False
            current_opener = user_state.current_opener
            current_opener.set_search_keyword(msg.content)
            response = current_opener.format_collection_list()
            await msg.reply(response)
            return
        
        # 检查是否是@机器人并发送"开箱"指令
        content = msg.content.strip()
        
        # 简单可靠的检测方式：检查消息中是否包含提及标签(met)和'开箱'关键词
        if '(met)' in content and '开箱' in content:
            # 视为/cs2 list指令
            current_opener = user_state.current_opener
            response = current_opener.format_collection_list()
            await msg.reply(response)
            return
        
        # 不返回任何值，让其他消息处理器继续处理

# 示例用法
if __name__ == "__main__":
    opener = CS2CaseOpener()
    
    # 显示收藏品列表
    print(opener.format_collection_list())
    
    # 示例：搜索收藏品
    # opener.set_search_keyword("命悬一线")
    # print(opener.format_collection_list())
    
    # 示例：开箱
    # collection = opener.get_collection_by_name("命悬一线武器箱")
    # if collection:
    #     results = opener.open_case(collection)
    #     print(opener.format_opening_result(collection["name"], results))
