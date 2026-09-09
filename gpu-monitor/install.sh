#!/usr/bin/env bash
# MTT GPU 监控看板 · 一键部署脚本
#
# 用法:
#   sudo bash install.sh            # 默认 8080 端口
#   sudo bash install.sh 8899       # 指定端口
#
# 可选环境变量:
#   SVC_NAME      systemd 服务名（默认 gpu-monitor）
#   INSTALL_DIR   安装目录（默认 /opt/gpu-monitor）
#   BIND_ADDR     监听地址（默认 0.0.0.0）
set -euo pipefail

PORT="${1:-8080}"
SVC_NAME="${SVC_NAME:-gpu-monitor}"
INSTALL_DIR="${INSTALL_DIR:-/opt/gpu-monitor}"
BIND_ADDR="${BIND_ADDR:-0.0.0.0}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { echo -e "\033[1;32m[部署]\033[0m $*"; }
die() { echo -e "\033[1;31m[错误]\033[0m $*" >&2; exit 1; }

# ---------------- 环境检查 ----------------
command -v python3 >/dev/null 2>&1 || die "未找到 python3，请先安装（需要 ≥ 3.7）"
python3 - <<'EOF' || die "Python 版本过低（需要 ≥ 3.7）"
import sys
sys.exit(0 if sys.version_info >= (3, 7) else 1)
EOF

command -v mthreads-gmi >/dev/null 2>&1 || \
    die "未找到 mthreads-gmi：本服务需要 MTT 显卡服务器（已安装 MUSA 驱动）"

command -v systemctl >/dev/null 2>&1 || \
    die "未找到 systemctl：请参考 README「无 systemd 环境」一节用 nohup 手动运行"

if [ "$(id -u)" -ne 0 ]; then
    say "安装 systemd 服务需要 root 权限，使用 sudo 重新执行…"
    exec sudo env SVC_NAME="$SVC_NAME" INSTALL_DIR="$INSTALL_DIR" \
        BIND_ADDR="$BIND_ADDR" bash "$0" "$@"
fi

if command -v ss >/dev/null 2>&1 && ss -tln 2>/dev/null | grep -q ":${PORT} "; then
    die "端口 ${PORT} 已被占用，请换一个端口: sudo bash install.sh <端口>"
fi

# ---------------- 安装 ----------------
say "安装到 ${INSTALL_DIR}（服务名 ${SVC_NAME}，端口 ${PORT}）"
mkdir -p "$INSTALL_DIR"
cp "$SRC_DIR/server.py" "$INSTALL_DIR/server.py"

cat > "/etc/systemd/system/${SVC_NAME}.service" <<EOF
[Unit]
Description=MTT GPU Monitor Dashboard (mthreads-gmi web)
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/server.py --host ${BIND_ADDR} --port ${PORT}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${SVC_NAME}" >/dev/null 2>&1
systemctl restart "${SVC_NAME}"

# ---------------- 健康检查（不依赖 curl） ----------------
ok=0
for _ in $(seq 1 15); do
    sleep 1
    if python3 - <<EOF 2>/dev/null
import sys, urllib.request
sys.exit(0 if urllib.request.urlopen(
    "http://127.0.0.1:${PORT}/", timeout=2).status == 200 else 1)
EOF
    then ok=1; break; fi
done

if [ "$ok" != 1 ]; then
    journalctl -u "${SVC_NAME}" -n 20 --no-pager || true
    die "服务启动失败，请查看上方日志排查"
fi

say "部署成功！"
echo
echo "========================================================"
echo "  MTT GPU 监控看板已上线，浏览器访问："
hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | \
    sed "s|^|    http://|; s|\$|:${PORT}|"
echo
echo "  服务管理："
echo "    systemctl status  ${SVC_NAME}"
echo "    systemctl restart ${SVC_NAME}"
echo "    systemctl stop    ${SVC_NAME}"
echo "    journalctl -u ${SVC_NAME} -f"
echo "========================================================"
