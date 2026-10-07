# -*- coding: utf-8 -*-
"""冒烟测试：验证桌面上部署的新 exe 修复了大视频读取。"""
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request

EXE = r"C:\Users\macro\Desktop\MetaForge\MetaForge.exe"
PORT = 8975
proc = subprocess.Popen([EXE, "--no-browser", "--port", str(PORT)])
try:
    ok = False
    for _ in range(50):
        time.sleep(0.3)
        import socket
        with socket.socket() as s:
            s.settimeout(0.3)
            if s.connect_ex(("127.0.0.1", PORT)) == 0:
                ok = True
                break
    print("server up:", ok)

    big = r"G:\数据文件\work\2026-10-06-12-41-19\metaforge\tests\_repro\t2_big_moov_end.mp4"
    with urllib.request.urlopen(
            "http://127.0.0.1:%d/api/meta?path=%s" % (PORT, urllib.parse.quote(big))) as r:
        m = json.loads(r.read().decode())
    print("kind:", m.get("kind"))
    print("basic:", m.get("basic"))
    print("title:", m["ilst"].get("©nam", {}).get("value"))
    print("tracks:", m.get("tracks"))
    # 标题值不写死：测试资产会被其他测试反复改写，只断言能读到非空标题
    title = m["ilst"].get("©nam", {}).get("value")
    verdict = (m.get("kind") == "video"
               and isinstance(title, str) and title
               and "时长" in m.get("basic", {}))
    print("SMOKE:", "PASS" if verdict else "FAIL")
    sys.exit(0 if verdict else 1)
finally:
    proc.terminate()
