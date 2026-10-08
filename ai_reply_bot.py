# -*- coding: utf-8 -*-
"""
抖音私信 AI 自动回复机器人（Termux 版，带 Web 控制台）
================================================================
复用 cv-cat/DouYin_Spider 的私信收发能力（WebSocket 收 + protobuf 发），
AI 回复走任意 OpenAI 兼容接口（本地 Ollama / 各类中转 API 都行）。

部署（详见 termux_install.sh）：
  1) 装好 Termux 环境、克隆 DouYin_Spider 项目、pip 装依赖
  2) python termux_patch.py    # Termux 装不了 curl_cffi，切换到纯 requests 回退
  3) python ai_reply_bot.py    # 启动后浏览器打开面板，网页里扫码登录

Web 控制台（默认开，端口 8765）：
  手机本机:  http://127.0.0.1:8765
  局域网:    http://<手机IP>:8765 （启动日志会打印）
  功能：
  - 登录页：显示二维码，抖音 App 扫码即登录，凭证自动存 .env
  - 消息页：实时看私信（文本/图片/语音/表情）与 AI 回复，一键开关自动回复
  - 设置页：在线改 AI 地址/Key/模型/人设/白名单/回复冷却，自动持久化到 .env

配置（环境变量，写进 .env 或直接在网页设置页改）：
  AI_BASE_URL          AI 接口地址，默认 http://127.0.0.1:11434/v1 （Ollama）
  AI_API_KEY           API Key，默认 ollama
  AI_MODEL             模型名，默认 qwen2.5:3b
  AI_SYSTEM_PROMPT     机器人人设
  REPLY_WHITELIST      逗号分隔的抖音用户数字ID；留空 = 回复所有人
  REPLY_COOLDOWN       同一人两次回复最小间隔（秒），默认 20
  AUTO_REPLY_ON_ERROR  AI 出错时是否兜底回复（true/false），默认 false
  WEB_ENABLED          是否开 Web 面板，默认 true
  PORT                 Web 面板端口，默认 8765
  AUTO_REPLY           启动时自动回复开关，默认 true

注意：
  - 频繁自动回复有触发平台风控的风险，默认已加同人冷却 + 全局速率限制。
  - Web 面板监听 0.0.0.0，同一局域网内任何设备都能打开，别在公共网络用。
"""
import json
import logging
import os
import socket
import threading
import time
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor


def _preflight():
    """启动前检查第三方依赖，缺了给安装提示而不是一堆 traceback。"""
    missing = []
    for mod in ("dotenv", "flask", "requests", "qrcode", "loguru", "websocket",
                "ecdsa", "cryptography", "blackboxprotobuf",
                "google.protobuf", "bs4"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print("缺少 Python 依赖: " + ", ".join(missing))
        print("请先执行安装（Termux 建议先 pkg install python-cryptography protobuf cmake clang）:")
        print("    pip install --break-system-packages -r requirements-termux.txt")
        raise SystemExit(1)


_preflight()

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("dybot")

# ---------------- 配置（网页设置页可改，改完写回 .env） ----------------
AI_BASE_URL = os.getenv("AI_BASE_URL", "http://127.0.0.1:11434/v1").rstrip("/")
AI_API_KEY = os.getenv("AI_API_KEY", "ollama")
AI_MODEL = os.getenv("AI_MODEL", "qwen2.5:3b")
SYSTEM_PROMPT = os.getenv(
    "AI_SYSTEM_PROMPT",
    "你是我的抖音私信小助手。规则：\n"
    "1. 回复要简短口语化，每次不超过100字\n"
    "2. 不要发代码块、不要发长文、不要用markdown格式\n"
    "3. 像微信聊天一样自然回复\n"
    "4. 遇到不确定的事就说需要问本人\n"
    "5. 不要编造事实",
)
WHITELIST = {x.strip() for x in os.getenv("REPLY_WHITELIST", "").split(",") if x.strip()}
REPLY_COOLDOWN = int(os.getenv("REPLY_COOLDOWN", "20"))
AUTO_REPLY_ON_ERROR = os.getenv("AUTO_REPLY_ON_ERROR", "false").lower() == "true"
WEB_ENABLED = os.getenv("WEB_ENABLED", "true").lower() == "true"
PORT = int(os.getenv("PORT", "8765"))
AUTO_REPLY = os.getenv("AUTO_REPLY", "true").lower() == "true"

MAX_AI_WORKERS = 3          # AI 并发上限
GLOBAL_MIN_INTERVAL = 3     # 全局两次发送最小间隔（秒）

START_TS = time.time()
AUTH = None                 # 登录态，main 里赋值

# ---------------- 运行期状态 ----------------
LOG = deque(maxlen=500)         # 消息/事件流水（Web 面板展示用）
LOG_TOTAL = 0
_seen_messages = set()          # (conversation_id, index) 去重
_seen_order = []                # FIFO 队列，满了删最旧的
_sms_last_sent = {}             # 手机号 -> 上次发送时间戳（频控）
_conv_cache = {}                # 对方uid -> (conversation_id, short_id, ticket)
_last_reply_at = {}             # 对方uid -> 上次回复时间戳
_global_lock = threading.Lock()
_global_last_send = 0.0
_ai_pool = ThreadPoolExecutor(max_workers=MAX_AI_WORKERS)
_my_uid = None

# ---------------- 网页登录状态 ----------------
LOGIN_STATE = {"status": "idle", "qr_svg": "", "msg": "",
               "sms_sent": False, "sms_phone": ""}
LOGIN_EVENT = threading.Event()
LOGIN_RESULT = {}
SMS_AUTH = None
SMS_LOCK = threading.Lock()


def append_log(entry):
    """记录一条流水（in/out/sys），同时打到终端。"""
    global LOG_TOTAL
    LOG_TOTAL += 1
    entry = {"i": LOG_TOTAL, "ts": time.strftime("%m-%d %H:%M:%S"), **entry}
    LOG.append(entry)
    txt = str(entry.get("text", ""))
    if entry.get("dir") == "in":
        logger.info(f"← [{entry.get('uid', '?')}] {entry.get('type', '')}: {txt[:60]}")
    elif entry.get("dir") == "out":
        logger.info(f"→ AI回复 [{entry.get('uid', '?')}]: {txt[:60]}")
    else:
        logger.info(f"· {txt}")


# ---------------- 登录 ----------------
def get_auth():
    """有 .env 凭证就直接用；没有返回 None（由 Web 页扫码登录接管）。"""
    if not os.getenv("DY_COOKIES"):
        return None
    from builder.auth import DouyinAuth
    auth = DouyinAuth.open(bootstrap_creator=False)
    if not (auth.cookie or {}).get("sessionid"):
        return None
    return auth


def _qr_to_svg(url):
    """把扫码链接画成内联 SVG（不依赖 Pillow，Termux 零额外安装）。"""
    import qrcode
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    mat = qr.get_matrix()
    n = len(mat)
    cells = "".join(
        f"<rect x='{x}' y='{y}' width='1' height='1'/>"
        for y, row in enumerate(mat) for x, v in enumerate(row) if v
    )
    return (
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {n} {n}' "
        f"shape-rendering='crispEdges' style='width:230px;height:230px;"
        f"background:#fff'><rect width='{n}' height='{n}' fill='#fff'/>"
        f"{cells}</svg>"
    )


def start_web_qr_login():
    """后台跑扫码登录；二维码实时推到 Web 页。"""
    def on_qrcode(url):
        if LOGIN_STATE.get("sms_sent"):
            return  # 用户已改用验证码登录，别覆盖它的状态
        LOGIN_STATE["qr_svg"] = _qr_to_svg(url)
        LOGIN_STATE["qr_url"] = url
        LOGIN_STATE["status"] = "new_qr"
        LOGIN_STATE["msg"] = "请用抖音 App 扫一扫（可截图后用抖音扫一扫识别）"

    def worker():
        try:
            from dy_apis.login_api import DYLoginApi
            api = DYLoginApi()
            auth = api.qrcode_login(timeout=300, show_qr=False, on_qrcode=on_qrcode)
            LOGIN_RESULT["auth"] = auth
            LOGIN_STATE["status"] = "confirmed"
            LOGIN_STATE["msg"] = "登录成功！正在进入…"
            logger.info("网页扫码登录成功")
        except Exception as exc:
            if not LOGIN_RESULT.get("auth"):  # 验证码已登录成功时不覆盖
                LOGIN_STATE["status"] = "error"
                LOGIN_STATE["msg"] = f"登录失败: {exc}"
                logger.error(f"网页扫码登录失败: {exc}")
        finally:
            LOGIN_EVENT.set()

    LOGIN_STATE.update(status="waiting", msg="正在获取二维码…", qr_svg="",
                       qr_url="")
    threading.Thread(target=worker, daemon=True).start()


def web_qr_login(timeout=360):
    """阻塞等待登录完成（扫码或验证码任一路径），返回 auth。"""
    if LOGIN_RESULT.get("auth"):
        return LOGIN_RESULT["auth"]
    LOGIN_EVENT.clear()
    start_web_qr_login()
    LOGIN_EVENT.wait(timeout)
    auth = LOGIN_RESULT.get("auth")
    if not auth:
        raise RuntimeError("网页登录失败: " + LOGIN_STATE.get("msg", "超时"))
    return auth


def my_uid():
    global _my_uid
    if _my_uid is None:
        if AUTH is None:
            return 0
        try:
            _my_uid = int(AUTH.get_uid() or 0)
        except Exception as exc:
            logger.warning(f"获取自身 uid 失败（不影响收发，仅用于过滤自己的消息）: {exc}")
            _my_uid = 0
    return _my_uid


# ---------------- AI 调用（标准库，不额外装包） ----------------
_chat_history = {}  # uid -> [{"role":"user"/"assistant","content":...}, ...]
_HISTORY_MAX = 20   # 每用户最多保留 20 条消息（10 轮对话）


def ask_ai(uid, user_text):
    """调 OpenAI 兼容 /chat/completions，带上该用户最近对话历史；失败返回空串。"""
    url = AI_BASE_URL + "/chat/completions"
    history = _chat_history.get(uid, [])
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history[-_HISTORY_MAX:])
    messages.append({"role": "user", "content": user_text})
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "temperature": 0.7,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + AI_API_KEY,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        logger.error(f"AI 调用失败（{AI_BASE_URL} / {AI_MODEL}）: {exc}")
        return ""


# ---------------- 配置读写（网页设置页用） ----------------
def mask_key(key):
    return (key[:4] + "****") if len(key) > 8 else "****"


def config_payload():
    return {
        "ai_base_url": AI_BASE_URL,
        "ai_api_key": mask_key(AI_API_KEY),
        "ai_model": AI_MODEL,
        "system_prompt": SYSTEM_PROMPT,
        "whitelist": ",".join(sorted(WHITELIST)),
        "reply_cooldown": REPLY_COOLDOWN,
        "auto_reply_on_error": AUTO_REPLY_ON_ERROR,
    }


def update_config(data):
    """网页提交的配置：改全局变量 + 持久化到 .env。"""
    global AI_BASE_URL, AI_API_KEY, AI_MODEL, SYSTEM_PROMPT
    global WHITELIST, REPLY_COOLDOWN, AUTO_REPLY_ON_ERROR
    changed = []

    def set_env(name, value):
        os.environ[name] = str(value)
        changed.append(name)

    if "ai_base_url" in data and str(data["ai_base_url"]).strip():
        AI_BASE_URL = str(data["ai_base_url"]).strip().rstrip("/")
        set_env("AI_BASE_URL", AI_BASE_URL)
    if ("ai_api_key" in data and str(data["ai_api_key"]).strip()
            and not str(data["ai_api_key"]).startswith("****")):
        AI_API_KEY = str(data["ai_api_key"]).strip()
        set_env("AI_API_KEY", AI_API_KEY)
    if "ai_model" in data and str(data["ai_model"]).strip():
        AI_MODEL = str(data["ai_model"]).strip()
        set_env("AI_MODEL", AI_MODEL)
    if "system_prompt" in data:
        SYSTEM_PROMPT = str(data["system_prompt"])
        set_env("AI_SYSTEM_PROMPT", SYSTEM_PROMPT)
    if "whitelist" in data:
        WHITELIST = {x.strip() for x in str(data["whitelist"]).split(",") if x.strip()}
        set_env("REPLY_WHITELIST", ",".join(sorted(WHITELIST)))
    if "reply_cooldown" in data:
        try:
            REPLY_COOLDOWN = max(0, int(data["reply_cooldown"]))
            set_env("REPLY_COOLDOWN", REPLY_COOLDOWN)
        except (TypeError, ValueError):
            pass
    if "auto_reply_on_error" in data:
        AUTO_REPLY_ON_ERROR = bool(data["auto_reply_on_error"])
        set_env("AUTO_REPLY_ON_ERROR", str(AUTO_REPLY_ON_ERROR).lower())

    if changed:
        try:
            from dotenv import set_key
            env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
            for name in changed:
                set_key(env_path, name, os.environ[name])
        except Exception as exc:
            logger.warning(f"配置写 .env 失败（仅本次运行生效）: {exc}")
        append_log({"dir": "sys", "text": "AI 配置已更新: " + ", ".join(changed)})
    return config_payload()


# ---------------- 收发 ----------------
def extract_uid(sender):
    """从 protobuf sender 里取对方数字 uid，供 create_conversation 用。"""
    if sender is None:
        return None
    # sender 本身就是 int64 数字（MessageBody.sender 字段类型=3 int64）
    if isinstance(sender, int):
        return sender
    if isinstance(sender, str):
        s = sender.strip()
        if s.isdigit():
            return int(s)
        return None
    for name in ("id", "user_id", "uid"):
        v = getattr(sender, name, None)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    try:
        from google.protobuf.json_format import MessageToDict
        d = MessageToDict(sender, preserving_proto_field_name=True)
        for name in ("id", "user_id", "uid"):
            v = d.get(name)
            if v:
                return int(v)
    except Exception:
        pass
    return None


def _url_of(content, key, subkey):
    try:
        node = (content or {}).get(key) or {}
        lst = node.get(subkey) or []
        return str(lst[0]) if lst else ""
    except Exception:
        return ""


def reply_to(uid, user_text):
    """生成 AI 回复并发送；带会话缓存、同人冷却、全局节流。"""
    now = time.time()
    if now - _last_reply_at.get(uid, 0) < REPLY_COOLDOWN:
        return
    _last_reply_at[uid] = now

    reply = ask_ai(uid, user_text)
    if not reply:
        append_log({"dir": "sys", "text": f"[{uid}] AI 未产出回复，跳过"})
        # AI 失败也记用户消息，保持上下文
        h = _chat_history.setdefault(uid, [])
        h.append({"role": "user", "content": user_text})
        if len(h) > _HISTORY_MAX:
            del h[:len(h) - _HISTORY_MAX]
        if not AUTO_REPLY_ON_ERROR:
            return
        reply = "稍等，我本人看到了会回复你～"

    from dy_apis.douyin_api import DouyinAPI

    global _global_last_send
    with _global_lock:
        wait = GLOBAL_MIN_INTERVAL - (time.time() - _global_last_send)
        if wait > 0:
            time.sleep(wait)
        _global_last_send = time.time()

    conv = _conv_cache.get(uid)
    try:
        if conv is None:
            conv = DouyinAPI.create_conversation(AUTH, uid)
            _conv_cache[uid] = conv
        conversation_id, short_id, ticket = conv
        # 长回复兜底分段（正常AI回复已限制在100字内）
        MAX_LEN = 300
        chunks = [reply[i:i+MAX_LEN] for i in range(0, len(reply), MAX_LEN)] or [reply]
        all_ok = True
        for chunk in chunks:
            ok = DouyinAPI.send_msg(AUTH, conversation_id, short_id, ticket, chunk)
            if not ok:
                all_ok = False
                break
            time.sleep(3)
        if all_ok:
            append_log({"dir": "out", "uid": uid, "type": "ai", "text": reply})
            # 记录对话历史（用户消息+AI回复）
            h = _chat_history.setdefault(uid, [])
            h.append({"role": "user", "content": user_text})
            h.append({"role": "assistant", "content": reply})
            if len(h) > _HISTORY_MAX:
                del h[:len(h) - _HISTORY_MAX]
        else:
            _conv_cache.pop(uid, None)      # 发送失败：下次重建会话再试
            append_log({"dir": "sys", "text": f"[{uid}] 发送失败"})
            # 发送失败也记用户消息，保持上下文
            h = _chat_history.setdefault(uid, [])
            h.append({"role": "user", "content": user_text})
            if len(h) > _HISTORY_MAX:
                del h[:len(h) - _HISTORY_MAX]
    except Exception as exc:
        import traceback
        _conv_cache.pop(uid, None)
        tb = traceback.format_exc()
        append_log({"dir": "sys", "text": f"[{uid}] 回复异常: {exc}"})
        logger.error(f"[{uid}] 回复异常完整堆栈:\n{tb}")


def handle_text(notify, content, uid=None):
    """收到一条文本私信后的回复入口（自动回复开关、白名单、去重都在这）。"""
    if not AUTO_REPLY:
        return
    if uid is None:
        uid = extract_uid(notify.sender)
    if not uid:
        return
    text = (content or {}).get("text", "").strip()
    if not text:
        return
    if str(uid) == str(my_uid()):
        return                                          # 自己发的（WS 会回显）
    if WHITELIST and str(uid) not in WHITELIST:
        append_log({"dir": "sys", "text": f"[{uid}] 不在白名单，跳过"})
        return
    key = (notify.conversation_id, notify.index_in_conversation)
    if key in _seen_messages:
        return
    _seen_messages.add(key)
    _seen_order.append(key)
    if len(_seen_order) > 20000:                     # FIFO 淘汰最旧的
        old = _seen_order.pop(0)
        _seen_messages.discard(old)
    _ai_pool.submit(reply_to, uid, text)


def start_receiver(auth):
    from static import Live_pb2, Response_pb2
    from dy_apis.douyin_recv_msg import DouyinRecvMsg

    class AIChatReceiver(DouyinRecvMsg):
        def on_open(self, ws):
            super().on_open(ws)
            append_log({"dir": "sys", "text": "已连接私信通道"})

        def on_close(self, ws, close_status_code, close_msg):
            super().on_close(ws, close_status_code, close_msg)
            append_log({"dir": "sys", "text": "连接断开，等待重连…"})

        def on_message(self, ws, message):
            try:
                frame = Live_pb2.PushFrame()
                frame.ParseFromString(message)
                if frame.payloadType != "pb":
                    return
                resp = Response_pb2.Response()
                resp.ParseFromString(frame.payload)
                notify = resp.body.new_message_notify.message
                if not notify:
                    return
                content = json.loads(notify.content)
                uid = extract_uid(notify.sender)
                uid_display = uid or "?"
                mtype = notify.message_type
                if mtype == 7:                          # 文本
                    append_log({"dir": "in", "uid": uid_display, "type": "私信",
                                "text": (content or {}).get("text", "") or "(空消息)"})
                    handle_text(notify, content, uid)
                elif mtype == 5:                        # 表情包
                    append_log({"dir": "in", "uid": uid_display, "type": "表情包",
                                "text": _url_of(content, "url", "url_list")})
                elif mtype == 27:                       # 图片
                    append_log({"dir": "in", "uid": uid_display, "type": "图片",
                                "text": _url_of(content, "resource_url", "origin_url_list")})
                elif mtype == 17:                       # 语音
                    append_log({"dir": "in", "uid": uid_display, "type": "语音",
                                "text": _url_of(content, "resource_url", "url_list")})
                elif mtype == 8:                        # 分享视频
                    append_log({"dir": "in", "uid": uid_display, "type": "分享视频",
                                "text": str((content or {}).get("itemId", ""))})
                else:
                    append_log({"dir": "in", "uid": uid_display, "type": "消息",
                                "text": f"类型 {mtype}"})
            except Exception as exc:
                logger.debug(f"消息解析跳过: {exc}")

    receiver = AIChatReceiver(auth, auto_reconnect=True)
    receiver.start()                                    # 阻塞，断线自动重连


# ---------------- Web 控制台 ----------------
def _lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def status():
    return {
        "login_required": AUTH is None,
        "my_uid": my_uid() or 0,
        "auto_reply": AUTO_REPLY,
        "ai_model": AI_MODEL,
        "ai_base_url": AI_BASE_URL,
        "total": LOG_TOTAL,
        "uptime": int(time.time() - START_TS),
        "pid": os.getpid(),
    }


PAGE_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>抖音私信 AI 机器人</title><style>
:root{color-scheme:dark}
body{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0f1115;color:#d7dae0;margin:0;padding:14px;max-width:780px;margin:0 auto}
h1{font-size:16px;color:#8b95a5;font-weight:600;margin:2px 0 8px}
h2{font-size:15px;color:#c6cdd9;margin:8px 0 6px}
.hint{color:#8b95a5;font-size:13px;line-height:1.6}
#qrBox{margin:14px 0;padding:10px;display:inline-block;background:#fff;border-radius:10px}
#loginMsg{color:#8b95a5;font-size:13px;margin-top:6px}
.ltabs{display:flex;gap:8px;margin-bottom:12px}
.ltabs button{flex:1;background:#151922;color:#8b95a5;border:1px solid #333a46;border-radius:8px;padding:8px 0;font-size:14px;cursor:pointer}
.ltabs button.active{background:#2a8a56;color:#fff;border-color:#2a8a56}
.row{margin:12px 0}
.row button{background:#2a8a56;color:#fff;border:none;border-radius:6px;padding:9px 14px;font-size:14px;cursor:pointer}
#smsMsg{color:#8b95a5;font-size:13px;margin-top:8px}
#head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:6px}
#uidLine{color:#5b6472;font-size:12px}
.tabs{display:flex;gap:8px;margin:10px 0}
.tabs button{background:#22262f;color:#8b95a5;border:1px solid #333a46;border-radius:6px;padding:4px 14px;font-size:13px;cursor:pointer}
.tabs button.active{background:#26314d;color:#8ab4ff;border-color:#3b4a6b}
button{background:#22262f;color:#d7dae0;border:1px solid #333a46;border-radius:6px;padding:4px 12px;font-size:13px;cursor:pointer}
button.on{background:#1f6f43;border-color:#2a8a56;color:#fff}
#toolbar{display:flex;gap:10px;align-items:center;margin-bottom:10px;font-size:13px;color:#8b95a5;flex-wrap:wrap}
#feed{border-top:1px solid #232833}
.msg{padding:8px 4px;border-bottom:1px solid #1b2029;font-size:14px;line-height:1.55;word-break:break-word}
.msg .ts{color:#5b6472;font-size:12px;margin-right:8px}
.msg .tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;margin-right:6px;vertical-align:1px}
.in .tag{background:#26314d;color:#8ab4ff}
.out .tag{background:#3d2c1e;color:#ffb86b}
.sys{color:#5b6472;font-size:13px}
.msg .uid{color:#8ab4ff;font-size:12px;margin-right:8px}
.out .uid{color:#ffb86b}
#cfgPane{max-width:560px}
label{display:block;font-size:12px;color:#8b95a5;margin:10px 0 4px}
input,textarea{width:100%;box-sizing:border-box;background:#151922;color:#d7dae0;border:1px solid #333a46;border-radius:6px;padding:7px 10px;font-size:14px;font-family:inherit}
textarea{min-height:70px;resize:vertical}
.check{display:flex;align-items:center;gap:8px;margin-top:12px;font-size:13px;color:#d7dae0}
.check input{width:auto}
#cfgMsg{color:#2a8a56;font-size:13px;margin-left:8px}
#saveBtn{margin-top:16px}
</style></head><body>

<div id="loginView">
  <div class="ltabs">
    <button id="ltabQr" class="active" onclick="switchLogin('qr')">扫码登录</button>
    <button id="ltabSms" onclick="switchLogin('sms')">验证码登录</button>
    <button id="ltabCk" onclick="switchLogin('ck')">粘贴Cookie</button>
  </div>
    <div id="qrPane">
    <h2>扫码登录抖音</h2>
    <p class="hint">用抖音 App「扫一扫」扫描下方二维码，在手机上确认登录。</p>
    <div id="qrBox"></div>
    <div id="qrUrl" class="hint"></div>
    <div id="loginMsg" class="hint">正在获取二维码…</div>
  </div>
  <div id="smsPane">
    <h2>手机号验证码登录</h2>
    <label>手机号（大陆 11 位）</label>
    <input id="smsPhone" placeholder="13800138000">
    <div class="row"><button onclick="sendSms()">发送验证码</button></div>
    <label>验证码（6 位）</label>
    <input id="smsCode" placeholder="输入收到的验证码" inputmode="numeric">
    <div class="row"><button onclick="submitSms()">登录</button></div>
    <div id="smsMsg" class="hint"></div>
  </div>
  <div id="ckPane" style="display:none">
    <h2>粘贴 Cookie 登录</h2>
    <p class="hint">用电脑浏览器登录 douyin.com（真实浏览器能过 2046），按 F12 打开开发者工具 → Network 任意请求 → 复制 Cookie 请求头，粘到下面（格式 k=v; k2=v2）。</p>
    <textarea id="ckInput" rows="6" placeholder="粘贴 cookie 字符串"></textarea>
    <div class="row"><button onclick="submitCk()">用 Cookie 登录</button></div>
    <div id="ckMsg" class="hint"></div>
  </div>
</div>

<div id="mainView" style="display:none">
  <div id="head">
    <h1>抖音私信 AI 机器人</h1>
    <span id="uidLine"></span>
  </div>
  <div class="tabs">
    <button id="tabMsg" class="active" onclick="showTab('msg')">消息</button>
    <button id="tabCfg" onclick="showTab('cfg')">设置</button>
  </div>
  <div id="msgPane">
    <div id="toolbar"><button id="toggle">自动回复: 开</button><span id="status"></span></div>
    <div id="feed"></div>
  </div>
  <div id="cfgPane" style="display:none">
    <h2>AI 配置</h2>
    <label>接口地址（Ollama: http://127.0.0.1:11434/v1，也可填中转 API）</label>
    <input id="cfg_ai_base_url" placeholder="http://127.0.0.1:11434/v1">
    <label>API Key（不填则保持不变）</label>
    <input id="cfg_ai_api_key" placeholder="ollama">
    <label>模型名</label>
    <input id="cfg_ai_model" placeholder="qwen2.5:3b">
    <label>人设提示词</label>
    <textarea id="cfg_system_prompt"></textarea>
    <label>白名单（抖音用户ID，逗号分隔；留空 = 回复所有人）</label>
    <input id="cfg_whitelist" placeholder="123456, 789012">
    <label>同人回复冷却（秒）</label>
    <input id="cfg_reply_cooldown" type="number" min="0">
    <div class="check"><input id="cfg_auto_reply_on_error" type="checkbox">
      <label for="cfg_auto_reply_on_error" style="margin:0">AI 出错时兜底回复</label></div>
    <button id="saveBtn" onclick="saveConfig()">保存配置</button><span id="cfgMsg"></span>
  </div>
</div>

<script>
let after=0;
function render(items){
  const feed=document.getElementById('feed');
  for(const m of items){
    const div=document.createElement('div');div.className='msg '+m.dir;
    const ts=document.createElement('span');ts.className='ts';ts.textContent=m.ts;div.appendChild(ts);
    if(m.dir==='in'){const t=document.createElement('span');t.className='tag';t.textContent=m.type||'私信';div.appendChild(t);}
    if(m.dir==='out'){const t=document.createElement('span');t.className='tag';t.textContent='AI回复';div.appendChild(t);}
    if(m.uid){const u=document.createElement('span');u.className='uid';u.textContent=m.uid;div.appendChild(u);}
    const txt=document.createElement('span');txt.textContent=m.text||'';div.appendChild(txt);
    feed.appendChild(div);
  }
  window.scrollTo(0,document.body.scrollHeight);
}
async function refresh(){
  try{
    const s=await (await fetch('/api/status')).json();
    if(s.login_required){
      document.getElementById('loginView').style.display='block';
      document.getElementById('mainView').style.display='none';
      const st=await (await fetch('/api/login/status')).json();
      if(st.qr_svg){document.getElementById('qrBox').innerHTML=st.qr_svg;}
      document.getElementById('loginMsg').textContent=st.msg||'请扫码';
      document.getElementById('qrUrl').textContent=(st.qr_url||'')?
        '扫不了？长按复制这行链接，粘到抖音 App 里打开试试：\\n'+st.qr_url:'';
      document.getElementById('smsMsg').textContent=st.msg||'';
      if(st.status==='idle'){fetch('/api/login/start',{method:'POST'});}
      return;
    }
    document.getElementById('loginView').style.display='none';
    document.getElementById('mainView').style.display='block';
    document.getElementById('uidLine').textContent='UID: '+(s.my_uid||'?');
    const btn=document.getElementById('toggle');
    btn.textContent='自动回复: '+(s.auto_reply?'开':'关');
    btn.classList.toggle('on',s.auto_reply);
    document.getElementById('status').textContent=s.ai_model+' @ '+s.ai_base_url;
    const d=await (await fetch('/api/messages?after='+after)).json();
    if(d.items.length){after=d.total;render(d.items);}
  }catch(e){
    const el=document.getElementById('loginMsg');
    if(el){el.textContent='页面加载失败，请用系统浏览器（Chrome/Edge/夸克）打开本页：'+e;}
  }
}
function showTab(t){
  document.getElementById('tabMsg').classList.toggle('active',t==='msg');
  document.getElementById('tabCfg').classList.toggle('active',t==='cfg');
  document.getElementById('msgPane').style.display=t==='msg'?'block':'none';
  document.getElementById('cfgPane').style.display=t==='cfg'?'block':'none';
  if(t==='cfg') loadConfig();
}
async function loadConfig(){
  const c=await (await fetch('/api/config')).json();
  document.getElementById('cfg_ai_base_url').value=c.ai_base_url;
  document.getElementById('cfg_ai_api_key').value=c.ai_api_key;
  document.getElementById('cfg_ai_model').value=c.ai_model;
  document.getElementById('cfg_system_prompt').value=c.system_prompt;
  document.getElementById('cfg_whitelist').value=c.whitelist;
  document.getElementById('cfg_reply_cooldown').value=c.reply_cooldown;
  document.getElementById('cfg_auto_reply_on_error').checked=c.auto_reply_on_error;
}
async function saveConfig(){
  const payload={
    ai_base_url:document.getElementById('cfg_ai_base_url').value.trim(),
    ai_model:document.getElementById('cfg_ai_model').value.trim(),
    system_prompt:document.getElementById('cfg_system_prompt').value,
    whitelist:document.getElementById('cfg_whitelist').value,
    reply_cooldown:document.getElementById('cfg_reply_cooldown').value,
    auto_reply_on_error:document.getElementById('cfg_auto_reply_on_error').checked,
  };
  const key=document.getElementById('cfg_ai_api_key').value.trim();
  if(key && !key.startsWith('****')) payload.ai_api_key=key;
  const r=await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const res=await r.json();
  document.getElementById('cfgMsg').textContent='已保存 ✓';
  setTimeout(()=>document.getElementById('cfgMsg').textContent='',2500);
  document.getElementById('cfg_ai_api_key').value=res.ai_api_key;
}
function switchLogin(t){
  document.getElementById('ltabQr').classList.toggle('active',t==='qr');
  document.getElementById('ltabSms').classList.toggle('active',t==='sms');
  document.getElementById('ltabCk').classList.toggle('active',t==='ck');
  document.getElementById('qrPane').style.display=t==='qr'?'block':'none';
  document.getElementById('smsPane').style.display=t==='sms'?'block':'none';
  document.getElementById('ckPane').style.display=t==='ck'?'block':'none';
}
async function submitCk(){
  const ck=document.getElementById('ckInput').value.trim();
  const out=document.getElementById('ckMsg');
  if(!ck){out.textContent='请先粘贴 Cookie';return;}
  const r=await fetch('/api/login/cookie',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cookie:ck})});
  const res=await r.json();
  out.textContent=res.msg||'';
}
async function sendSms(){
  const phone=document.getElementById('smsPhone').value.trim();
  const out=document.getElementById('smsMsg');
  if(!phone){out.textContent='请输入手机号';return;}
  const r=await fetch('/api/login/sms/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone})});
  const res=await r.json();
  out.textContent=res.msg||'';
}
async function submitSms(){
  const phone=document.getElementById('smsPhone').value.trim();
  const code=document.getElementById('smsCode').value.trim();
  const out=document.getElementById('smsMsg');
  if(!phone||!code){out.textContent='请输入手机号和验证码';return;}
  const r=await fetch('/api/login/sms/submit',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone,code})});
  const res=await r.json();
  out.textContent=res.msg||'';
}
document.getElementById('toggle').onclick=async function(){
  const now=this.textContent.includes('开');
  await fetch('/api/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({auto_reply:!now})});
  refresh();
};
refresh();setInterval(refresh,2000);
</script></body></html>"""


def start_web_server(port):
    """后台起一个 Flask 控制台；装不了 flask 就跳过，不影响收发。"""
    try:
        from flask import Flask, jsonify, request
    except ImportError:
        logger.warning("未安装 flask（pip install flask），跳过 Web 面板")
        return None

    app = Flask(__name__)
    WEB_PASSWORD = os.getenv("WEB_PASSWORD", "")

    @app.before_request
    def _check_auth():
        if not WEB_PASSWORD:
            return None  # 没设密码 = 本地用不锁
        from flask import request, make_response
        if request.path == "/api/login/password":
            return None
        token = request.cookies.get("bot_auth", "")
        if token == WEB_PASSWORD:
            return None
        # 未登录：API 返回 401，页面显示登录框
        if request.path.startswith("/api/"):
            return jsonify({"error": "unauthorized"}), 401
        login_html = (
            '<html><body style="font-family:sans-serif;background:#111;color:#fff;'
            'display:flex;align-items:center;justify-content:center;height:100vh;">'
            '<form method="post" action="/api/login/password" style="text-align:center;">'
            '<h3>控制台密码</h3>'
            '<input name="pwd" type="password" autofocus style="padding:8px;margin:8px;width:200px;">'
            '<br><button style="padding:8px 20px;">登录</button></form></body></html>'
        )
        return login_html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.post("/api/login/password")
    def _login_password():
        from flask import request, make_response
        pwd = request.form.get("pwd", "")
        if pwd == WEB_PASSWORD:
            resp = make_response(jsonify({"ok": True}))
            resp.set_cookie("bot_auth", WEB_PASSWORD, max_age=86400*30)
            return resp
        return jsonify({"ok": False}), 401

    @app.get("/")
    def index():
        # 服务端直接把二维码渲染进 HTML：即使手机浏览器 JS 受限/禁用，
        # 打开页面也能看到二维码，不会黑屏；JS 正常时再由 refresh() 覆盖。
        html = PAGE_HTML
        if LOGIN_STATE.get("qr_svg"):
            html = html.replace(
                '<div id="qrBox"></div>',
                '<div id="qrBox">%s</div>' % LOGIN_STATE["qr_svg"])
        if LOGIN_STATE.get("qr_url"):
            from html import escape as _esc
            html = html.replace(
                '<div id="qrUrl" class="hint"></div>',
                '<div id="qrUrl" class="hint">扫不了？长按复制这行链接，'
                '粘到抖音 App 里打开试试：\n%s</div>' % _esc(LOGIN_STATE["qr_url"]))
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.get("/api/messages")
    def api_messages():
        after = request.args.get("after", 0, type=int)
        items = [e for e in LOG if e["i"] > after]
        return jsonify({"total": LOG_TOTAL, "items": items})

    @app.get("/api/status")
    def api_status():
        return jsonify(status())

    @app.post("/api/control")
    def api_control():
        global AUTO_REPLY
        data = request.get_json(force=True, silent=True) or {}
        if "auto_reply" in data:
            AUTO_REPLY = bool(data["auto_reply"])
            append_log({"dir": "sys",
                        "text": f"自动回复已{'开启' if AUTO_REPLY else '关闭'}（网页操作）"})
        return jsonify({"auto_reply": AUTO_REPLY})

    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_payload())

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(force=True, silent=True) or {}
        return jsonify(update_config(data))

    @app.get("/api/login/status")
    def api_login_status():
        return jsonify(LOGIN_STATE)

    @app.post("/api/login/start")
    def api_login_start():
        if AUTH is not None:
            return jsonify({"status": "already", "msg": "已登录"})
        if LOGIN_STATE["status"] in ("waiting", "new_qr", "scanned", "confirmed"):
            return jsonify(LOGIN_STATE)
        start_web_qr_login()
        return jsonify(LOGIN_STATE)

    def _sms_worker(phone, code):
        """发验证码（code=None）或提交验证码登录，结果写回全局状态。"""
        global SMS_AUTH
        try:
            from builder.auth import DouyinAuth
            from dy_apis.login_api import DYLoginApi
            if code is None:
                auth, resp = DouyinAuth.open(
                    login_type="phone", phone=phone, bootstrap_creator=False)
                SMS_AUTH = auth
                LOGIN_STATE.update(status="sms_sent", sms_sent=True,
                                   sms_phone=phone,
                                   msg="验证码已发送，请输入 6 位验证码登录")
                append_log({"dir": "sys", "text": f"验证码已发送至 {phone}"})
            else:
                if SMS_AUTH is None:
                    raise RuntimeError("请先发送验证码，再提交登录")
                auth = DouyinAuth.open(
                    login_type="phone", phone=phone, code=code,
                    auth=SMS_AUTH, bootstrap_creator=False)
                DYLoginApi().save_credential(auth)
                LOGIN_RESULT["auth"] = auth
                LOGIN_STATE.update(status="confirmed", msg="登录成功！正在进入…")
                append_log({"dir": "sys", "text": "验证码登录成功，凭证已保存"})
                LOGIN_EVENT.set()
        except Exception as exc:
            LOGIN_STATE.update(status="sms_error",
                               msg=f"验证码登录失败: {exc}")
            logger.error(f"验证码登录失败: {exc}")

    @app.post("/api/login/sms/send")
    def api_login_sms_send():
        if AUTH is not None or LOGIN_RESULT.get("auth"):
            return jsonify({"ok": False, "msg": "已登录"})
        data = request.get_json(force=True, silent=True) or {}
        phone = str(data.get("phone", "")).strip()
        if not phone:
            return jsonify({"ok": False, "msg": "请输入手机号"})
        # 频控：同号 60 秒内只能发一次
        now = time.time()
        last = _sms_last_sent.get(phone, 0)
        if now - last < 60:
            wait = int(60 - (now - last))
            return jsonify({"ok": False, "msg": f"发送太频繁，请 {wait} 秒后再试"})
        _sms_last_sent[phone] = now
        if LOGIN_STATE["status"] == "sms_sent":
            return jsonify({"ok": True, "msg": "验证码已发送，请查收"})
        LOGIN_STATE.update(status="sms_sending", msg="正在发送验证码…")
        threading.Thread(target=_sms_worker, args=(phone, None),
                         daemon=True).start()
        return jsonify({"ok": True, "msg": "正在发送验证码…"})

    @app.post("/api/login/sms/submit")
    def api_login_sms_submit():
        if AUTH is not None or LOGIN_RESULT.get("auth"):
            return jsonify({"ok": False, "msg": "已登录"})
        data = request.get_json(force=True, silent=True) or {}
        phone = str(data.get("phone", "")).strip()
        code = str(data.get("code", "")).strip()
        if not phone or not code:
            return jsonify({"ok": False, "msg": "请输入手机号和验证码"})
        LOGIN_STATE.update(status="sms_submitting", msg="正在登录…")
        threading.Thread(target=_sms_worker, args=(phone, code),
                         daemon=True).start()
        return jsonify({"ok": True, "msg": "正在登录…"})

    @app.post("/api/login/cookie")
    def api_login_cookie():
        """粘贴已有登录 Cookie（真实浏览器会话，可绕过扫码 2046 风控）。"""
        if AUTH is not None or LOGIN_RESULT.get("auth"):
            return jsonify({"ok": False, "msg": "已登录"})
        data = request.get_json(force=True, silent=True) or {}
        cookie = str(data.get("cookie", "")).strip().replace("\n", "").replace("\r", "").replace("\\n", "")
        if not cookie:
            return jsonify({"ok": False, "msg": "请粘贴 Cookie"})
        LOGIN_STATE.update(status="ck_logging", msg="正在用 Cookie 登录…")

        def worker():
            try:
                from builder.auth import DouyinAuth
                auth = DouyinAuth.open(
                    cookie_str=cookie, login_type="cookie",
                    bootstrap_creator=False)
                if not (auth.cookie or {}).get("sessionid"):
                    raise RuntimeError(
                        "Cookie 里没有 sessionid（未真正登录成功或复制的不是"
                        "登录态 Cookie），请重新登录 douyin.com 后复制")
                # cookie 登录没经过 passport bootstrap，需要本地生成 ECDSA 密钥对
                if not auth.private_key:
                    from utils.passport import generate_ec_keypair, build_client_data_cookie
                    auth.private_key, _pub = generate_ec_keypair()
                    import base64 as _b64
                    auth.ree_public_key = _b64.b64encode(
                        auth.private_key.encode()).decode()
                    auth.cookie["bd_ticket_guard_client_data"] = build_client_data_cookie(auth.private_key)
                    auth._sync_cookie_str()
                    append_log({"dir": "sys", "text": "已生成本地 ECDSA 密钥对"})
                from dy_apis.login_api import DYLoginApi
                DYLoginApi().save_credential(auth)
                LOGIN_RESULT["auth"] = auth
                LOGIN_STATE.update(status="confirmed",
                                   msg="Cookie 登录成功！正在进入…")
                append_log({"dir": "sys", "text": "Cookie 登录成功，凭证已保存"})
                LOGIN_EVENT.set()
            except Exception as exc:
                LOGIN_STATE.update(status="ck_error",
                                   msg=f"Cookie 登录失败: {exc}")
                logger.error(f"Cookie 登录失败: {exc}")

        threading.Thread(target=worker, daemon=True).start()
        return jsonify({"ok": True, "msg": "正在登录…"})

    threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False,
                               use_reloader=False, threaded=True),
        daemon=True,
    ).start()
    logger.info(f"Web 控制台: http://127.0.0.1:{port}  （局域网: http://{_lan_ip()}:{port}）")
    return app


if __name__ == "__main__":
    if WEB_ENABLED:
        start_web_server(PORT)

    AUTH = get_auth()
    if AUTH is None:
        logger.info("未检测到本地登录凭证，请在 Web 控制台扫码登录")
        append_log({"dir": "sys", "text": "等待网页扫码登录…"})
        # 登录失败不退出：Web 控制台保持在线，自动重试（每次都会刷新二维码）
        while True:
            try:
                AUTH = web_qr_login()
                break
            except Exception as exc:
                msg = str(exc)
                # error_code=2046 = 抖音要求先去 App 内完成账号安全验证。
                # 狂刷二维码没用（扫了也会被拦），暂停久一点，提示用户先处理。
                if "2046" in msg or "前往抖音APP完成验证" in msg:
                    LOGIN_STATE.update(
                        status="risk",
                        msg="抖音风控：请先在抖音 App 内完成账号安全验证"
                            "（退出重登/滑块/短信任一种），完成后本页会自动出新码")
                    logger.error(
                        f"抖音要求先完成账号安全验证（error_code=2046），"
                        f"暂停 60 秒再出码: {exc}")
                    time.sleep(60)
                else:
                    logger.error(f"网页扫码登录失败，10 秒后自动重试: {exc}")
                    time.sleep(10)
        try:
            from dy_apis.login_api import DYLoginApi
            DYLoginApi().save_credential(AUTH)
            logger.info("登录凭证已写入 .env，下次启动免扫码")
        except Exception as exc:
            logger.warning(f"凭证落盘失败（不影响本次运行）: {exc}")
    else:
        logger.info("已使用 .env 本地登录凭证")

    # cookie 登录/凭证加载后，如果没有 private_key，本地生成 ECDSA 密钥对
    if AUTH and not AUTH.private_key:
        try:
            from utils.passport import generate_ec_keypair, build_client_data_cookie
            AUTH.private_key, _pub = generate_ec_keypair()
            import base64 as _b64
            AUTH.ree_public_key = _b64.b64encode(AUTH.private_key.encode()).decode()
            AUTH.cookie["bd_ticket_guard_client_data"] = build_client_data_cookie(AUTH.private_key)
            AUTH._sync_cookie_str()
            logger.info("已生成本地 ECDSA 密钥对（cookie 登录无 bootstrap）")
        except Exception as exc:
            logger.warning(f"ECDSA 密钥对生成失败: {exc}")

    logger.info(f"登录成功，我的 uid={my_uid()}，AI: {AI_MODEL} @ {AI_BASE_URL}")
    append_log({"dir": "sys", "text": "机器人已启动，开始监听私信"})
    try:
        start_receiver(AUTH)
    except KeyboardInterrupt:
        logger.info("已退出")
    except Exception as exc:
        logger.error(f"接收器启动失败: {exc}")
        logger.error("常见原因：登录态过期（Web 面板或删掉 .env 后重新扫码）、网络问题、"
                     "或 requests 回退被风控拦截（稍后重试 / 检查回复频率）")
