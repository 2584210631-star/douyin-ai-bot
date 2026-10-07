#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# 抖音私信 AI 自动回复机器人 - Termux 一键安装脚本
# 在仓库根目录运行：bash termux_install.sh
# ============================================================
set -e
cd "$(dirname "$0")"

[ -d utils ] || { echo "未找到 utils/ 目录，请在仓库根目录运行本脚本"; exit 1; }

echo "==> [1/4] 基础环境检查"
if command -v python >/dev/null 2>&1; then
  echo "python 已就绪: $(python --version 2>&1)"
else
  echo "未找到 python，用 pkg 安装……"
  echo "（若 pkg update 报 Hash Sum mismatch：pkg clean && rm -rf \$PREFIX/var/lib/apt/lists/* && pkg update -y）"
  echo "（若 tur 源持续报错且本机不需要：pkg remove -y tur-repo）"
  pkg update -y || true
  pkg install -y python python-pip clang libcurl openssl \
    || { echo "基础包安装失败，请先修复 pkg 再重试"; exit 1; }
fi

echo "==> [2/4] 安装预编译原生依赖 + 编译工具"
# cryptography/protobuf 在 Termux 没有 pip 预编译轮子，用 pkg 提供：
#   python-cryptography —— 预编译版，避免 pip 用 Rust 编译
#   protobuf + cmake + ninja + clang —— 让 pip 能编译 protobuf>=5.27（pb2 强制要求）
pkg install -y python-cryptography protobuf cmake ninja clang 2>/dev/null || true

echo "==> [3/4] 安装精简依赖（跳过 curl_cffi/opencv/av 等装不动的）"
# 国内网络慢可加镜像：-i https://pypi.tuna.tsinghua.edu.cn/simple/
pip install --break-system-packages -r requirements-termux.txt || \
pip install -r requirements-termux.txt

echo "==> [4/4] 打补丁并启动（浏览器打开 http://127.0.0.1:8765 扫码登录）"
python termux_patch.py
python ai_reply_bot.py
