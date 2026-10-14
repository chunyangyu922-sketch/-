@echo off
chcp 936 >nul
title 数据库一键导入工具
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [出错] 未检测到 Python。
  echo   请先双击「一键安装Python.bat」完成安装，
  echo   或从 python.org 下载安装（勾选 Add python.exe to PATH）。
  echo   刚装完 Python 请关闭本窗口后重新双击。
  echo.
  pause
  exit /b 1
)
python 数据库导入工具.py
if errorlevel 1 (
  echo.
  echo 导入工具异常退出，请把上方报错信息截图反馈。
  pause
)