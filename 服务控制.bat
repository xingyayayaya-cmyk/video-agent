@echo off
chcp 65001 > nul
title EduAgent 服务控制
echo.
echo ============================================================
echo            EduAgent 出海短视频审核平台 - 服务控制
echo ============================================================
echo.
echo   请选择操作：
echo.
echo     [1]  重启全部服务（数据库 + 后端 + 前端）
echo     [2]  只重启后端（改完 Python 代码后用这个）
echo     [3]  停止全部服务
echo     [4]  启动全部服务
echo     [0]  退出
echo.
set /p choice="   输入编号后回车："

if "%choice%"=="1" goto restart_all
if "%choice%"=="2" goto restart_backend
if "%choice%"=="3" goto stop_all
if "%choice%"=="4" goto start_all
goto end

:restart_all
echo.
echo   正在重启全部服务，请稍候（约 30 秒）...
powershell -NoProfile -Command "Restart-Service EduAgentFrontend -Force -ErrorAction SilentlyContinue; Restart-Service EduAgentBackend -Force; Write-Host '   后端已重启，等待就绪...'; Start-Sleep -Seconds 20; Get-Service pg-eduagent,EduAgentBackend,EduAgentFrontend | Format-Table @{N='服务';E={$_.Name}},Status -AutoSize"
goto done

:restart_backend
echo.
echo   正在重启后端，请稍候（约 25 秒）...
powershell -NoProfile -Command "Restart-Service EduAgentBackend -Force; Start-Sleep -Seconds 20; Get-Service EduAgentBackend | Format-Table @{N='服务';E={$_.Name}},Status -AutoSize"
goto done

:stop_all
echo.
echo   正在停止服务...
powershell -NoProfile -Command "Stop-Service EduAgentFrontend,EduAgentBackend -Force -ErrorAction SilentlyContinue; Stop-Service pg-eduagent -Force -ErrorAction SilentlyContinue; Get-Service pg-eduagent,EduAgentBackend,EduAgentFrontend | Format-Table @{N='服务';E={$_.Name}},Status -AutoSize"
goto done

:start_all
echo.
echo   正在启动服务...
powershell -NoProfile -Command "Start-Service pg-eduagent -ErrorAction SilentlyContinue; Start-Sleep -Seconds 5; Start-Service EduAgentBackend; Start-Sleep -Seconds 20; Start-Service EduAgentFrontend; Start-Sleep -Seconds 5; Get-Service pg-eduagent,EduAgentBackend,EduAgentFrontend | Format-Table @{N='服务';E={$_.Name}},Status -AutoSize"
goto done

:done
echo.
echo ------------------------------------------------------------
echo   完成。可运行「查看服务状态.bat」确认，或直接打开
echo   http://127.0.0.1:3000
echo ------------------------------------------------------------
echo.
pause
goto end

:end
