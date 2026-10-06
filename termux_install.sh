#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# 抖音私信 AI 自动回复机器人 - Termux 一键安装脚本
# 在仓库根目录运行：bash termux_install.sh
# ============================================================
set -e
cd "$(dirname "$0")"

[ -d utils ] || { echo "未找到 utils/ 目录，请在仓库根目录运行本脚本"; exit 1; }

echo "==> [1/4] 更新 Termux 并安装基础包"
pkg update -y
pkg install -y python python-pip clang libcurl openssl

echo "==> [2/4] 安装精简依赖（跳过 curl_cffi/opencv/av 等 Termux 装不动的）"
pip install --break-system-packages -r requirements-termux.txt || \
pip install -r requirements-termux.txt

echo "==> [3/4] 打 Termux 回退补丁（HTTP 层切纯 requests）"
python termux_patch.py

echo "==> [4/4] 启动机器人（启动后浏览器打开 http://127.0.0.1:8765 扫码登录）"
python ai_reply_bot.py
