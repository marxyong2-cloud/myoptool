@echo off
chcp 65001 >nul
title LLM API Benchmark Tool
cd /d "%~dp0"

echo ============================================================
echo   LLM API Benchmark Tool
echo   启动后自动打开浏览器;同一 Wi-Fi 下手机可扫码访问
echo ============================================================
echo.

rem ---- 已有 venv 且依赖完整,直接启动 ----
if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -c "import fastapi, aiohttp, openpyxl, uvicorn" >nul 2>&1
    if not errorlevel 1 goto :run
    echo 检测到虚拟环境已损坏,可能是从别的电脑复制的,正在重建...
    rmdir /s /q venv
)

rem ---- 寻找带 pip 的 Python ----
set PYEXE=
python -m pip --version >nul 2>&1 && set PYEXE=python
if not defined PYEXE (
    py -3 -m pip --version >nul 2>&1 && set PYEXE=py -3
)
if not defined PYEXE (
    python3 -m pip --version >nul 2>&1 && set PYEXE=python3
)
if not defined PYEXE (
    echo [错误] 未找到可用的 Python。
    echo 请先安装 Python 3.9+ : https://www.python.org/downloads/
    echo 安装时请勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

echo [1/2] 使用 %PYEXE% 创建虚拟环境...
%PYEXE% -m venv venv
if errorlevel 1 (
    echo [错误] 虚拟环境创建失败
    pause
    exit /b 1
)

echo [2/2] 安装依赖(仅首次需要,约 1-2 分钟)...
venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
    echo [错误] 依赖安装失败,请检查网络后重试
    pause
    exit /b 1
)

:run
echo.
venv\Scripts\python.exe server.py
pause
