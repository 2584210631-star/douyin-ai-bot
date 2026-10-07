# -*- coding: utf-8 -*-
"""
Termux 回退补丁：把 DouYin_Spider 的 HTTP 层从 curl_cffi 切换到纯 requests。
================================================================
背景：项目默认用 curl_cffi 冒充 Chrome 的 TLS/HTTP2 指纹；curl_cffi 只发布
glibc(manylinux) 轮子，Termux 是 bionic libc 装不上，直接 import 就会崩。
本补丁用 requests 写一个同名同接口的 shim 替换 utils/http_client.py，
原文件备份为 utils/http_client.py.orig。

用法（在项目根目录运行）：
    python termux_patch.py

说明：
  - 收私信（WebSocket）完全不受影响；发消息、登录等 HTTP 接口失去 Chrome
    TLS 指纹，可能更容易触发抖音风控，请保持脚本里的冷却/频控。
  - 若以后在普通 Linux/Windows 上跑，把 http_client.py.orig 恢复即可。
"""
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(PROJECT_DIR, "utils", "http_client.py")
BACKUP = TARGET + ".orig"

SHIM = '''# coding=utf-8
"""Termux 回退版 HTTP 出口（termux_patch.py 生成）。
原版用 curl_cffi 冒充 Chrome TLS 指纹，Termux 装不了 curl_cffi，
这里用纯 requests 保持同名接口，供项目其余模块无感调用。
"""
import threading
from contextlib import nullcontext

import requests as _requests

try:  # 关掉 verify=False 的告警噪音
    _requests.packages.urllib3.disable_warnings()
except Exception:
    pass

IMPERSONATE = "chrome"
DEFAULT_TIMEOUT = 30


def _clean(kwargs):
    """去掉 curl_cffi 专属参数，requests 不认识它们。"""
    for key in ("impersonate", "default_headers", "http_version",
                "split_cookie_header", "on_fresh_cookies"):
        kwargs.pop(key, None)
    return kwargs


def request(method, url, **kwargs):
    return _requests.request(method, url, **_clean(kwargs))


def get(url, **kwargs):
    return request("GET", url, **kwargs)


def post(url, **kwargs):
    return request("POST", url, **kwargs)


def put(url, **kwargs):
    return request("PUT", url, **kwargs)


def head(url, **kwargs):
    return request("HEAD", url, **kwargs)


def delete(url, **kwargs):
    return request("DELETE", url, **kwargs)


class Session:
    """持久会话；接口对齐原版，供登录/creator 链路使用。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._session = _requests.Session()

    def locked(self):
        return nullcontext()

    @property
    def cookies(self):
        return self._session.cookies

    def request(self, method, url, **kwargs):
        with self._lock:
            return self._session.request(method, url, **_clean(kwargs))

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def head(self, url, **kwargs):
        return self.request("HEAD", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def close(self):
        with self._lock:
            self._session.close()

    def cookie_header(self, url, method="GET"):
        try:
            return "; ".join(
                f"{c.name}={c.value}" for c in self._session.cookies
            )
        except Exception:
            return ""

    def request_with_cookie_header(self, method, url, cookie_header, **kwargs):
        with self._lock:
            headers = dict(kwargs.pop("headers", {}) or {})
            if not any(name.lower() == "cookie" for name in headers):
                headers["cookie"] = cookie_header
            return self._session.request(method, url, headers=headers,
                                         **_clean(kwargs))


class _Urllib3Shim:
    @staticmethod
    def disable_warnings(*args, **kwargs):
        return None


class _PackagesShim:
    urllib3 = _Urllib3Shim


packages = _PackagesShim()
exceptions = _requests.exceptions
'''


def main():
    if not os.path.exists(os.path.join(PROJECT_DIR, "utils")):
        print("未找到 utils/ 目录，请在 DouYin_Spider 项目根目录运行本脚本")
        sys.exit(1)
    if os.path.exists(TARGET) and not os.path.exists(BACKUP):
        os.rename(TARGET, BACKUP)
        print(f"已备份原文件 -> {BACKUP}")
    with open(TARGET, "w", encoding="utf-8") as f:
        f.write(SHIM)
    print(f"已写入 Termux 回退版 -> {TARGET}")


if __name__ == "__main__":
    main()
