# douyin-ai-bot

抖音私信 AI 自动回复机器人（Termux 版，带 Web 控制台）。基于 [cv-cat/DouYin_Spider](https://github.com/cv-cat/DouYin_Spider) 的私信收发能力（WebSocket 收 + protobuf 发 + 纯 Python a_bogus 签名），接入任意 OpenAI 兼容接口（本地 Ollama / 各类中转 API）自动回复。

## 目录结构（仓库根目录即项目，已展平）

| 路径 | 说明 |
|---|---|
| `ai_reply_bot.py` | 机器人主程序（Web 控制台：扫码登录 / 实时看私信 / AI 配置） |
| `termux_patch.py` | Termux 回退补丁（curl_cffi → 纯 requests） |
| `termux_install.sh` | Termux 一键安装脚本 |
| `requirements-termux.txt` | 精简依赖清单 |
| `builder/` `dy_apis/` `dy_live/` `static/` `utils/` `newsign/` 等 | 上游 DouYin_Spider 源码（已内置 Termux 回退补丁） |
| `README-upstream.md` | 上游项目原始 README |

## 快速开始（Termux）

```bash
bash termux_install.sh
# 启动后浏览器打开 http://127.0.0.1:8765
# 首次使用：网页里扫码登录 → 消息页看私信 → 设置页配 AI
```

主要配置（网页「设置」页可改，持久化到 `.env`）：

- `AI_BASE_URL` — AI 接口地址，默认 `http://127.0.0.1:11434/v1`（Ollama）
- `AI_MODEL` — 模型名，默认 `qwen2.5:3b`
- `REPLY_WHITELIST` — 白名单（抖音用户 ID，逗号分隔），留空回复所有人
- `REPLY_COOLDOWN` — 同人回复冷却（秒），默认 20

## 注意

- 仅供个人学习与技术研究使用；上游项目声明「严禁用于发布不良信息、违法内容」。
- 自动回复属平台禁止的自动化操作，有风控/封号风险，默认已带冷却与频控，请勿提高频率。
- 私信发送链路在 Termux 上因缺少 curl_cffi 的 Chrome TLS 指纹，可能更易触发风控。
