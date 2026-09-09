"""打包为可分发 zip:只包含部署必需文件,排除 venv/reports/缓存。"""
import os
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
INCLUDE = [
    "server.py",
    "requirements.txt",
    "start.bat",
    "start.sh",
    "README.md",
    "Dockerfile",
    ".dockerignore",
    "package.py",
    # 前端:主压测页 + ModelUse 工作台 + 模型部署站(Vue 3 + Element Plus + ECharts,零构建自托管)
    os.path.join("static", "index.html"),
    os.path.join("static", "modeluse.html"),
    os.path.join("static", "modelstart.html"),
    # 旧版原生 JS 页面(legacy,保留一个版本作回滚对照)
    os.path.join("static", "legacy-index.html"),
    os.path.join("static", "legacy-modeluse.html"),
    # Mock 上游(联调/冒烟测试用)
    os.path.join("tools", "mock_server.py"),
    # 注:工作台数据存于启动时自动创建的 modeluse.db(SQLite);
    # 技能/提示词种子数据已内置在 server.py,无需额外数据文件
    # GPU 监控部署包(部署到 MTT 显卡服务器,页面顶部「GPU监控」内嵌其看板)
    os.path.join("gpu-monitor", "server.py"),
    os.path.join("gpu-monitor", "install.sh"),
    os.path.join("gpu-monitor", "gpu-monitor.service"),
    os.path.join("gpu-monitor", "README.md"),
]
# 整目录包含:前端框架发行文件(固定版本)与 ES 模块;tests 为自测脚本(需先启动服务)
INCLUDE_DIRS = [
    os.path.join("static", "vendor"),
    os.path.join("static", "app"),
    "tests",
]
OUT = os.path.join(BASE, "llm-benchmark-tool.zip")

def iter_dir(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]   # 不打包字节码缓存
        for fn in filenames:
            if not fn.endswith((".pyc", ".pyo")):
                yield os.path.join(dirpath, fn)

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for rel in INCLUDE:
        src = os.path.join(BASE, rel)
        if os.path.exists(src):
            z.write(src, rel)
            print(f"  + {rel}")
        else:
            print(f"  ! 缺失 {rel}")
    for d in INCLUDE_DIRS:
        src_dir = os.path.join(BASE, d)
        if not os.path.isdir(src_dir):
            print(f"  ! 缺失目录 {d}")
            continue
        n = 0
        for p in iter_dir(src_dir):
            rel = os.path.relpath(p, BASE)
            z.write(p, rel)
            n += 1
        print(f"  + {d}{os.sep} ({n} 个文件)")

print(f"\n打包完成: {OUT} ({os.path.getsize(OUT)//1024} KB)")
