@echo off
title 资产管理系统服务器
cd /d "%~dp0"
echo 正在启动资产管理系统服务器...
python ams_server.py
if errorlevel 1 (
  echo.
  echo 启动出错。常见原因：
  echo   1. 未安装 Python：请先双击「一键安装Python.bat」
  echo   2. 端口被占用：编辑 ams_server.py 修改 PORT
  echo   3. 刚装完 Python：请关闭本窗口后重新双击
  echo.
  pause
)