@echo off
chcp 65001 > nul
title EduAgent 服务状态
echo.
echo ============================================================
echo            EduAgent 出海短视频审核平台 - 服务状态
echo ============================================================
echo.
powershell -NoProfile -Command "Get-Service pg-eduagent,EduAgentBackend,EduAgentFrontend -ErrorAction SilentlyContinue | Select-Object @{N='服务';E={$_.Name}},@{N='状态';E={$_.Status}},@{N='启动方式';E={$_.StartType}} | Format-Table -AutoSize"
echo ------------------------------------------------------------
echo   端口占用检查：
powershell -NoProfile -Command "foreach ($p in 5433,8000,3000) { $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue; if ($c) { Write-Host ('   ' + $p + ' : 监听中 OK') } else { Write-Host ('   ' + $p + ' : 未监听 !!') } }"
echo ------------------------------------------------------------
echo   页面地址：http://127.0.0.1:3000
echo   API 文档：http://127.0.0.1:8000/docs
echo ============================================================
echo.
pause
