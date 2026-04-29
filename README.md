# KOOK-DeepSeek 机器人

基于 [khl.py](https://github.com/TWT233/khl.py) 与 DeepSeek API 的 KOOK 平台聊天机器人。

## 功能

- 🤖 **AI 对话**:DeepSeek 双模型联动(`deepseek-chat` 工具判断 + `deepseek-reasoner` 深度推理),支持多轮上下文。
- 🔍 **联网搜索**:接入 Tavily,模型自主决策何时搜索,带安全审核回退。
- 📅 **签到积分**:`签到` / `qd` 每日签到 3-10 分,`qdlist` 查看排行榜。
- 🎤 **语音频道**:`进频道` / `离开` 让机器人加入或退出语音频道。
- 🎵 **音乐播放**:`听歌` / `music` 通过 ffmpeg RTP 推流播放网易云音乐。

## 环境要求

- Python 3.8+
- ffmpeg(音乐播放需要,加入系统 PATH)

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 配置环境变量
cp bot.env.example bot.env
# 编辑 bot.env,填入三个 key:
#   KOOK_BOT_TOKEN     - KOOK 开发者后台获取
#   DEEPSEEK_API_KEY   - https://platform.deepseek.com/
#   TAVILY_API_KEY     - https://tavily.com/

# 3. 运行
python bot.py
```

## 项目结构

```
.
├── bot.py                  # 主入口
├── kook_music.py           # 音乐播放模块(网易云 + ffmpeg RTP)
├── bot.env                 # 环境变量(本地,勿提交)
├── bot.env.example         # 环境变量模板
├── requirements.txt
├── data/                   # 运行时数据(自动创建)
│   ├── checkin_data.json   # 签到记录
│   └── user_database.json  # 用户信息缓存
├── logs/                   # 日志目录
├── tests/                  # 测试脚本
└── docs/                   # 项目与第三方文档
    ├── environment_requirements.md
    ├── kook-api-reference/  # KOOK 官方中文文档
    └── kook-openapi/        # KOOK OpenAPI 描述
```

## 命令

| 命令 | 说明 |
|---|---|
| `@机器人 <内容>` / 私聊 | AI 对话 |
| `签到` / `qd` | 每日签到 |
| `qdlist` | 签到排行榜 |
| `进频道` / `join` / `来` | 让机器人加入语音频道 |
| `离开` / `leave` / `退频道` | 让机器人离开语音频道 |
| `听歌` / `music` | 播放音乐 |

## 注意

- `bot.env` 和 `data/*.json` 已加入 `.gitignore`,不会被提交。
- 三个 API Key 全部从环境变量读取,代码中无硬编码。
- 仅支持 WebSocket 模式(本地长连接);如需 Webhook/Vercel 部署,需自行接入。

## 许可

MIT
