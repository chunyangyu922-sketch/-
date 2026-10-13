@echo off
title Python 一键安装（资产管理系统运行环境）
cd /d "%~dp0"

echo ================================================
echo    Python 3.12.10 一键安装（资产管理系统运行环境）
echo ================================================
echo.

set "PYVER="
for /f "tokens=*" %%v in ('python --version 2^>nul') do set "PYVER=%%v"
echo %PYVER% | findstr /b "Python" >nul
if %errorlevel%==0 (
  echo 检测到本机已安装：%PYVER%
  echo 无需重复安装。请直接双击「启动资产系统服务器.bat」。
  echo.
  pause
  exit /b
)

if not exist "python-3.12.10-amd64.exe" (
  echo 未找到安装包 python-3.12.10-amd64.exe，
  echo 请确认它与本脚本在同一文件夹内。
  echo.
  pause
  exit /b
)

echo 未检测到 Python，开始自动安装（约 1-3 分钟）...
echo   - 安装位置：当前用户目录（无需管理员权限）
echo   - 自动加入 PATH 环境变量
echo   - 安装过程中请勿关闭本窗口
echo.
start /wait "" "python-3.12.10-amd64.exe" /passive InstallAllUsers=0 PrependPath=1 Include_test=0

echo.
set "PYPATH=%LocalAppData%\Programs\Python\Python312\python.exe"
if exist "%PYPATH%" (
  "%PYPATH%" --version
  echo.
  echo [成功] Python 安装完成！
  echo.
  echo 下一步：
  echo   1. 关闭本窗口，重新打开一个新的文件夹窗口
  echo   2. 双击「启动资产系统服务器.bat」启动系统
  echo.
  echo 说明：环境变量对新开的窗口生效；若当前窗口
  echo 提示找不到 python，关闭后重新双击即可。
) else (
  echo [失败] 未检测到安装结果，可能被取消或安全软件拦截。
  echo   可手动双击 python-3.12.10-amd64.exe 安装，
  echo   安装首页务必勾选「Add python.exe to PATH」。
)
echo.
pause