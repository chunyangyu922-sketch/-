@echo off
chcp 936 >nul
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

if exist "python-3.12.10-amd64.exe" (
  echo 未检测到 Python，开始自动安装（约 1-3 分钟）...
  echo   - 安装位置：当前用户目录（无需管理员权限）
  echo   - 自动加入 PATH 环境变量
  echo   - 安装过程中请勿关闭本窗口
  echo.
  start /wait "" "python-3.12.10-amd64.exe" /passive InstallAllUsers=0 PrependPath=1 Include_test=0
  goto :check
)

echo 未找到安装包 python-3.12.10-amd64.exe（在线仓库不含大文件）。
echo 尝试使用 Windows 自带的 winget 在线安装...
where winget >nul 2>nul
if %errorlevel%==0 (
  winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements
) else (
  echo 未找到 winget，请手动安装 Python：
  echo   1. 访问 https://www.python.org/downloads/ 下载 Python 3.12 或更新版本
  echo   2. 安装首页务必勾选「Add python.exe to PATH」
  echo   3. 安装完成后，重新双击「启动资产系统服务器.bat」
  echo.
  start "" "https://www.python.org/downloads/"
  pause
  exit /b
)

:check
set "PYPATH=%LocalAppData%\Programs\Python\Python312\python.exe"
if exist "%PYPATH%" (
  "%PYPATH%" --version
  echo.
  echo [成功] Python 安装完成！
) else (
  where python >nul 2>nul
  if %errorlevel%==0 (
    python --version
    echo.
    echo [成功] Python 已可用！
  ) else (
    echo [失败] 未检测到安装结果，可能被取消或安全软件拦截。
    echo   可手动双击 python-3.12.10-amd64.exe 安装，
    echo   安装首页务必勾选「Add python.exe to PATH」。
    pause
    exit /b 1
  )
)
echo.
echo 下一步：
echo   1. 关闭本窗口，重新打开一个新的文件夹窗口
echo   2. 双击「启动资产系统服务器.bat」启动系统
echo.
echo 说明：环境变量对新开的窗口生效；若当前窗口
echo 提示找不到 python，关闭后重新双击即可。
echo.
pause