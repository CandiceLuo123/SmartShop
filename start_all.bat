@echo off
title 小铺智答 - 一键启动
cd /d "%~dp0"

echo ============================================
echo   小铺智答 一键启动（服务 + 公网隧道）
echo ============================================

REM 清理占用 8000 端口的旧服务，避免端口冲突
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8000.*LISTENING"') do (
    echo 清理旧服务进程 PID %%p ...
    taskkill /PID %%p /F >nul 2>&1
)

echo [1/2] 启动客服服务...
start "小铺智答-服务" cmd /k "python run_web.py --host 0.0.0.0 --port 8000"

echo [2/2] 启动公网隧道（新窗口会打印本次的公网网址）...
start "小铺智答-隧道" cmd /k "tools\cloudflared.exe tunnel --url http://localhost:8000 --no-autoupdate"

echo.
echo 正在等待隧道就绪（约 12 秒）...
timeout /t 12 >nul

echo ============================================
echo   本机访问:  http://localhost:8000
echo   公网访问:  查看"小铺智答-隧道"窗口中的
echo              https://xxxx.trycloudflare.com
echo   （每次重启公网网址都会变，属正常现象）
echo   关闭方法: 双击 stop_all.bat
echo ============================================
pause
