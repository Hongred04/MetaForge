# -*- coding: utf-8 -*-
"""自校验脚本：nonce=KX7Q2M。第一行必须输出该标记，否则运行结果不可信。"""
import json
import os
import sys
import time
import urllib.request

NONCE = "KX7Q2M-OK-BEGIN"
print(NONCE, flush=True)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import server  # noqa: E402

OK = []


def hr(title, cond):
    OK.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + title, flush=True)


# 1) 源码内容检查
srv = open(os.path.join(ROOT, "core", "server.py"), encoding="utf-8").read()
hr("server.py 有 /api/ping 路由", '"/api/ping"' in srv)
hr("server.py 有看门狗函数", "_page_watchdog" in srv and "_page_expired" in srv)
hr("server.py 有 os._exit(0)", "os._exit(0)" in srv)
hr("server.py 有 import time", "\nimport time" in srv)
js = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
hr("app.js 有 Worker 心跳", "new Worker" in js and "/api/ping" in js)
hr("app.js 有服务停止提示", "服务已退出" in js)

# 2) 真实 HTTP ping
httpd = server.serve(watch_page=False)
port = httpd.server_address[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping") as r:
    j = json.loads(r.read().decode("utf-8"))
hr("GET /api/ping -> ok", j.get("ok") is True)
hr("心跳登记 seen=True", server._page_state["seen"] is True)

# 3) 阈值判定
now = time.monotonic()
hr("刚心跳 → 未超时", server._page_expired(now) is False)
hr("未到 60s → 未超时", server._page_expired(now + 55) is False)
hr("超过 60s → 超时", server._page_expired(now + 65) is True)
with server._page_lock:
    server._page_state["seen"] = False
hr("从未见过页面 → 永不超时", server._page_expired(now + 999999) is False)
httpd.shutdown()

print(f"NONCE-END {sum(OK)}/{len(OK)} {'ALLPASS' if all(OK) else 'HASFAIL'}", flush=True)
sys.exit(0 if all(OK) else 1)
