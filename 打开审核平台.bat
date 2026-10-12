@echo off
chcp 65001 > nul
title EduAgent 出海短视频审核平台 - 打开
echo.
echo   正在打开审核平台...
echo   地址：http://127.0.0.1:3000
echo.
echo   登录账号：
echo     创作者  creator01@eduagent.local  /  Creator@123456
echo     审核员  reviewer01@eduagent.local /  Reviewer@123456
echo     管理员  admin@eduagent.local     /  Admin@123456
echo.
start "" http://127.0.0.1:3000
timeout /t 3 > nul
