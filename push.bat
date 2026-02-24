@echo off
cd /d "%~dp0"

echo 正在推送到 GitHub...
echo.

"C:\Program Files\Git\bin\git.exe" remote add origin https://github.com/realwhiter/123kook-bot.git 2>nul
"C:\Program Files\Git\bin\git.exe" push -u origin master

echo.
echo 完成！
pause
