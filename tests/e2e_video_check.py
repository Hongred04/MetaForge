# -*- coding: utf-8 -*-
"""端到端：真实 HTTP 服务验证大视频(moov 在尾)读取/预览/写入。"""
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import server as srv  # noqa: E402

OK = []


def hr(title, cond):
    OK.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + title)


def get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path)) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (PORT, path),
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8"))


httpd = srv.serve(None)
PORT = httpd.server_address[1]
print("server on", PORT)

repro = os.path.join(ROOT, "tests", "_repro")

# 扫描复现目录
folder = repro.replace("\\", "/")
r = get("/api/scan?folder=" + urllib.parse.quote(repro) + "&recursive=0")
mp4s = [f for f in r["files"] if f["name"].endswith(".mp4")]
hr(f"扫描到 {len(mp4s)} 个 mp4", len(mp4s) >= 5)

# 大视频(60MB moov 在尾)读取
big = os.path.join(repro, "t3_check.mp4") if os.path.exists(os.path.join(repro, "t3_check.mp4")) else os.path.join(repro, "t2_big_moov_end.mp4")
m = get("/api/meta?path=" + urllib.parse.quote(big))
hr("大视频 kind=video", m.get("kind") == "video")
hr("大视频读到标题", m["ilst"].get("©nam", {}).get("value", "").startswith(("原始", "一个", "长短")))
hr("大视频有时长", "时长" in m.get("basic", {}))

# 带封面的大视频：preview 应为 data URL
cov = os.path.join(repro, "t7_cover_big.mp4")
m2 = get("/api/meta?path=" + urllib.parse.quote(cov))
hr("带封面大视频 preview 是 data:image", bool(m2.get("preview")) and m2["preview"].startswith("data:image/"))

# 无封面小视频：preview 依赖 Shell（沙箱里会失败→None，不能崩）
plain = os.path.join(repro, "t1_small_faststart.mp4")
m3 = get("/api/meta?path=" + urllib.parse.quote(plain))
hr("无封面视频 meta 不报错", m3.get("ok") is True)
print("    (无封面视频 preview ==", m3.get("preview"), "==；沙箱内 Shell 不可用属预期)")

# 通过 HTTP 写入并回读
r = post("/api/write", {"paths": [big], "changes": {"©nam": "HTTP端到端标题"}, "backup": False})
hr("HTTP 写入成功", r.get("ok") is True)
m4 = get("/api/meta?path=" + urllib.parse.quote(big))
hr("HTTP 回读标题生效", m4["ilst"].get("©nam", {}).get("value") == "HTTP端到端标题")

# /api/thumb 视频分支不 500（有封面应 200）
try:
    with urllib.request.urlopen("http://127.0.0.1:%d/api/thumb?path=%s" % (PORT, urllib.parse.quote(cov))) as resp:
        hr("带封面视频 /api/thumb 200", resp.status == 200)
except Exception as e:
    hr("带封面视频 /api/thumb 200 (%s)" % e, False)

httpd.shutdown()
print(f"\n{'全部通过' if all(OK) else '存在失败'}: {sum(OK)}/{len(OK)}")
sys.exit(0 if all(OK) else 1)
