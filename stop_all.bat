@echo off
title 小铺智答 - 停止全部
echo 停止客服服务（8000 端口）...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000.*LISTENING"') do taskkill /PID %%p /F 2>nul
echo 停止公网隧道...
taskkill /IM cloudflared.exe /F 2>nul
echo 全部已停止。
pause
