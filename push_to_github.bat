@echo off
cd /d "%~dp0"

echo ========================================
echo 123kook-bot 项目推送到 GitHub
echo ========================================
echo.

REM 检查Git是否安装
where git >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo [错误] Git未安装，请先安装Git
    pause
    exit /b 1
)

REM 添加Git到PATH（如果需要）
set PATH=%PATH%;"C:\Program Files\Git\bin"

echo 步骤1: 在GitHub上创建仓库
echo.
echo 请在浏览器中打开以下链接创建新仓库:
echo https://github.com/new
echo.
echo 仓库名称: 123kook-bot
echo 描述(可选): Kook bot with DeepSeek AI integration
echo 设为 Public
echo 不要勾选 "Add a README file"
echo 不要勾选 "Add .gitignore"
echo.
echo 创建仓库后，按回车键继续...
pause >nul

echo.
echo 步骤2: 添加远程仓库
echo.
git remote add origin https://github.com/realwhiter/123kook-bot.git

echo.
echo 步骤3: 推送到GitHub
echo.
git push -u origin master

echo.
echo ========================================
echo 推送完成!
echo 仓库地址: https://github.com/realwhiter/123kook-bot
echo ========================================
pause
