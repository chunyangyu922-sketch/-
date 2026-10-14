@echo off
chcp 936 >nul
title 资产管理系统服务器
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [出错] 未检测到 Python。
  echo   请先双击「一键安装Python.bat」完成安装，
  echo   或从 python.org 下载安装（勾选 Add python.exe to PATH）。
  echo   刚装完 Python 请关闭本窗口后重新双击。
  echo.
  pause
  exit /b 1
)
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