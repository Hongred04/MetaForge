# -*- coding: utf-8 -*-
"""页面心跳自动退出：/api/ping 端点 + 看门狗判定逻辑 + app.js 心跳落地检查。"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import server  # noqa: E402

OK = []


def hr(title, cond):
    OK.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + title)


# 1) /api/ping 端点（watch_page=False 防止测试进程被看门狗杀掉）
httpd = server.serve(watch_page=False)
port = httpd.server_address[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping") as r:
    j = json.loads(r.read().decode("utf-8"))
hr("/api/ping 返回 ok", j.get("ok") is True)
hr("心跳已登记 seen=True", server._page_state["seen"] is True)

# 2) 判定逻辑
now = time.monotonic()
hr("刚心跳过 → 未超时", server._page_expired(now) is False)
hr(f"心跳后 {server.PAGE_CLOSE_EXIT_SECONDS - 5:.0f}s → 未超时",
   server._page_expired(now + server.PAGE_CLOSE_EXIT_SECONDS - 5) is False)
hr(f"心跳后 {server.PAGE_CLOSE_EXIT_SECONDS + 5:.0f}s → 超时",
   server._page_expired(now + server.PAGE_CLOSE_EXIT_SECONDS + 5) is True)

# 3) 从未见心跳 → 永不超时（--no-browser / 脚本化使用不受影响）
with server._page_lock:
    server._page_state["seen"] = False
hr("从未见过页面 → 永不超时", server._page_expired(now + 999999) is False)
with server._page_lock:          # 恢复状态供后续断言
    server._page_state["seen"] = True
    server._page_state["last"] = now
httpd.shutdown()

# 4) app.js 心跳代码是否真实落盘
js = open(os.path.join(ROOT, "web", "app.js"), encoding="utf-8").read()
hr("app.js 含 Worker 心跳", "/api/ping" in js and "new Worker" in js)
hr("app.js 含服务停止提示", "服务已退出" in js)

# 5) run.py 用默认 serve()（看门狗默认开启）
run_src = open(os.path.join(ROOT, "run.py"), encoding="utf-8").read()
hr("run.py 调用 serve(port)（默认 watch_page=True）",
   "server.serve(port" in run_src or "server.serve(" in run_src)

print(f"\n{'全部通过' if all(OK) else '存在失败'}: {sum(OK)}/{len(OK)}")
sys.exit(0 if all(OK) else 1)
