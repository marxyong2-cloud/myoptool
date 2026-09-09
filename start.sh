#!/bin/bash
# LLM API Benchmark Tool - macOS / Linux 启动脚本
cd "$(dirname "$0")"

echo "============================================================"
echo "  LLM API Benchmark Tool"
echo "  启动后自动打开浏览器;同一 Wi-Fi 下手机可扫码访问"
echo "============================================================"
echo ""

# 已有 venv 且依赖完整,直接启动(损坏则重建)
if [ -x "venv/bin/python" ]; then
    if ! venv/bin/python -c "import fastapi, aiohttp, openpyxl, uvicorn" 2>/dev/null; then
        echo "检测到虚拟环境已损坏(可能是从别的电脑复制的),正在重建..."
        rm -rf venv
    fi
fi

if [ ! -d "venv" ]; then
    # 寻找带 pip 的 Python
    PYBIN=""
    for c in python3 python; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -m pip --version >/dev/null 2>&1; then
            PYBIN="$c"
            break
        fi
    done
    if [ -z "$PYBIN" ]; then
        echo "[错误] 未找到可用的 Python,请先安装 Python 3.9+:"
        echo "  macOS:  brew install python3   或 https://www.python.org/downloads/"
        exit 1
    fi
    echo "[1/2] 使用 $PYBIN 创建虚拟环境..."
    "$PYBIN" -m venv venv || { echo "[错误] 虚拟环境创建失败"; exit 1; }

    echo "[2/2] 安装依赖(仅首次需要,约 1-2 分钟)..."
    venv/bin/python -m pip install -r requirements.txt || {
        echo "[错误] 依赖安装失败,请检查网络后重试"
        exit 1
    }
fi

echo ""
venv/bin/python server.py
