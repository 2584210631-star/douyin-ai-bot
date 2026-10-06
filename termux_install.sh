#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# 抖音私信 AI 自动回复机器人 - Termux 一键部署脚本
# 用法：bash termux_install.sh
# ============================================================
set -e

echo "==> [1/5] 更新 Termux 并安装基础包"
pkg update -y
pkg install -y python python-pip git clang libcurl openssl

echo "==> [2/5] 克隆 DouYin_Spider（GitHub 拉不动可换镜像："
echo "        ghproxy 前缀：https://ghproxy.net/https://github.com/cv-cat/DouYin_Spider"
echo "        或电脑下载 zip 传到手机）"
if [ ! -d DouYin_Spider ]; then
  git clone https://github.com/cv-cat/DouYin_Spider.git
fi
cd DouYin_Spider

echo "==> [3/5] 安装精简依赖（跳过 curl_cffi/opencv/av 等 Termux 装不动的）"
# 装脚本同目录的 requirements-termux.txt（本脚本所在目录）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
pip install --break-system-packages -r "$SCRIPT_DIR/requirements-termux.txt" || \
pip install -r "$SCRIPT_DIR/requirements-termux.txt"

echo "==> [4/5] 打 Termux 回退补丁（HTTP 层切纯 requests）"
cp "$SCRIPT_DIR/termux_patch.py" ./
python termux_patch.py

echo "==> [5/5] 启动机器人（首次会打印二维码，用抖音 App 扫一下；"
echo "        启动后浏览器打开 http://127.0.0.1:8765 看私信面板）"
cp "$SCRIPT_DIR/ai_reply_bot.py" ./
python ai_reply_bot.py
