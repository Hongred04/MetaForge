# -*- coding: utf-8 -*-
"""端到端验证打包 exe 的页面心跳自动退出。

A 进程：收到一次 /api/ping 后模拟网页关闭（不再 ping）→ 约 64 秒内应自行退出。
B 进程：从未 ping（--no-browser 且无页面打开）→ 应一直存活。
两进程并行跑，总耗时约 75 秒。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

EXE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "dist", "MetaForge", "MetaForge.exe")

OK = []


def hr(title, cond):
    OK.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + title, flush=True)


def start(port):
    p = subprocess.Popen([EXE, "--no-browser", "--port", str(port)])
    for _ in range(60):
        time.sleep(0.25)
        import socket
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return p
    raise RuntimeError(f"端口 {port} 未就绪")


a = start(8976)
b = start(8977)

with urllib.request.urlopen("http://127.0.0.1:8976/api/ping") as r:
    j = json.loads(r.read().decode("utf-8"))
hr("exe 的 /api/ping 返回 ok", j.get("ok") is True)

deadline = time.time() + 75
a_gone_at = None
while time.time() < deadline:
    if a.poll() is not None and a_gone_at is None:
        a_gone_at = time.time()
    time.sleep(1)
hr("A（心跳停止）约 64s 内自动退出", a.poll() is not None)
if a_gone_at:
    hr(f"A 实际存活 {a_gone_at - (deadline - 75):.0f}s（阈值 60s+两拍确认）",
       a.poll() is not None)
hr("B（从未见过页面）75s 后仍存活", b.poll() is None)

b.terminate()
sys.exit(0 if all(OK) else 1)
