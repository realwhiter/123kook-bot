# 使用终端运行脚本所需的环境列表

以下是使用终端运行Kook机器人脚本的完整环境要求：

## 1. 核心环境
- **Python版本**：Python 3.11或兼容版本（推荐3.11.9）
- **终端类型**：支持Windows命令的终端环境（PowerShell或CMD）
- **网络连接**：稳定的网络连接（用于访问Kook API、DeepSeek API和Tavily搜索服务）

## 2. Python依赖库
| 库名称 | 用途 | 版本要求 |
|--------|------|----------|
| khl.py | 与Kook平台交互 | 0.3.17+ |
| openai | 调用DeepSeek API | 2.15.0+ |
| python-dotenv | 加载.env文件中的环境变量 | 1.2.1+ |
| tavily-python | 执行互联网搜索 | 0.7.19+ |
| starlette | 构建ASGI应用（支持Vercel部署） | 0.52.0+ |

## 3. 配置文件
- **bot.env**：包含机器人运行所需的所有配置项
  - `KOOK_BOT_TOKEN`：Kook机器人的身份令牌
  - `DEEPSEEK_API_KEY`：DeepSeek API的访问密钥
  - `TAVILY_API_KEY`：Tavily搜索服务的API密钥
  - `VERIFY_TOKEN`（可选）：Webhook验证令牌（仅Webhook模式使用）
  - `ENCRYPT_KEY`（可选）：Webhook加密密钥（仅Webhook模式使用）

## 4. 权限要求
- 终端具有读取项目文件的权限
- 终端具有网络访问权限
- 对于Windows系统，可能需要管理员权限（首次安装Python时）

## 5. 运行环境说明

### 本地运行
- **WebSocket模式**：默认使用，适合本地测试
- **Webhook模式**：需配置`VERIFY_TOKEN`，适合服务器部署

### 部署选项
- **本地部署**：直接在本地终端运行
- **Vercel部署**：支持云部署，需配置Webhook

## 如何安装环境

### 1. 安装Python
- 下载地址：https://www.python.org/downloads/
- 安装时勾选"Add Python to PATH"选项（方便直接使用python命令）

### 2. 安装依赖库
```bash
python -m pip install -r requirements.txt
```

### 3. 配置环境变量
- 编辑`bot.env`文件，填入正确的API密钥和令牌

## 运行脚本命令

### 单个机器人
```bash
python kook_deepseek_bot.py
```
或
```bash
python kook_deepseek_bot_ljmm.py
```

### 同时运行两个机器人
```bash
python start_bots.py
```

## 常见问题

1. **Python命令未找到**：检查Python是否已添加到系统PATH
2. **依赖库版本冲突**：使用虚拟环境或指定版本安装
3. **API调用失败**：检查网络连接和API密钥是否正确
4. **WebSocket连接失败**：检查Kook机器人令牌是否正确

## 环境验证

运行以下命令验证Python和依赖库是否正确安装：

```bash
python --version
python -m pip list | grep -E "khl\.py|openai|python-dotenv|tavily-python|starlette"
```