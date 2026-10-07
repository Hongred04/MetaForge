"""服务层端到端测试：真实 HTTP 请求覆盖全部接口。"""
import io
import json
import os
import shutil
import struct
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import server  # noqa: E402
from PIL import Image  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
WORK = os.path.join(HERE, "_svctest")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))


def setup():
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    for n in os.listdir(ASSETS):
        if n != "sample_photo.tif":     # tif 太大太慢，跳过
            shutil.copy2(os.path.join(ASSETS, n), os.path.join(WORK, n))
    # 构造几张待重命名的图
    for i in range(3):
        Image.new("RGB", (32, 24), (i * 40, 80, 160)).save(
            os.path.join(WORK, f"untitled_{i}.png"))


PORT = server.find_free_port()
httpd = server.serve(PORT)
BASE = f"http://127.0.0.1:{PORT}"


def get(path_, **params):
    """返回 (status, parsed_body)。非 2xx 也会返回而不是抛异常。"""
    url = BASE + path_
    if params:
        url += "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            ct = r.headers.get("content-type", "")
            body = r.read()
            return r.status, (json.loads(body) if "json" in ct else body)
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def get_json(path_, **params):
    st, body = get(path_, **params)
    if st != 200:
        raise AssertionError(f"{path_} 返回 {st}: {str(body)[:200]}")
    return body


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


try:
    setup()

    # ---------- 静态资源 ----------
    html = get_json("/")
    check("静态-首页可访问", b"MetaForge" in html, str(html[:80]))
    check("静态-CSS", b"--accent" in get_json("/style.css"))
    check("静态-JS", b"function renderAll" in get_json("/app.js"))
    st_tr, _ = get("/../run.py")
    check("静态-目录穿越被拒", st_tr == 404, f"status={st_tr}")

    # ---------- 字段清单 ----------
    f = get_json("/api/fields")
    check("API-字段清单", len(f["exif_editable"]) > 5 and len(f["presets"]) == 3)
    check("API-重命名变量", "创建日期" in f["rename_vars"])

    # ---------- 扫描 ----------
    sc = get_json("/api/scan", folder=WORK)
    check("API-扫描", sc["ok"] and sc["count"] >= 5, f"count={sc.get('count')}")
    img_p = os.path.join(WORK, "sample_photo.jpg")
    vid_p = os.path.join(WORK, "sample_video.mp4")

    # ---------- 读取 ----------
    m = get_json("/api/meta", path=img_p)
    check("API-读取图片", m["ok"] and m["kind"] == "image")
    check("API-图片有预览图数据", (m.get("preview") or "").startswith("data:image"))
    mv = get_json("/api/meta", path=vid_p)
    check("API-读取视频", mv["ok"] and mv["kind"] == "video")
    check("API-视频ilst", mv["ilst"].get("©nam", {}).get("value") == "原始标题")

    # ---------- 写入图片 ----------
    w = post("/api/write", {
        "paths": [img_p],
        "changes": {"相机制造商": "API测试", "相机型号": "MF-1"},
        "gps": {"lat": 31.2304, "lon": 121.4737, "alt": 8.5},
        "backup": True,
    })
    check("API-写入图片成功", w["ok"], str(w))
    m2 = get_json("/api/meta", path=img_p)
    check("API-图片EXIF已生效",
          m2["exif"].get("相机制造商 [0th:271]") == "API测试", str(m2["exif"])[:100])
    check("API-图片GPS已生效",
          m2.get("gps_decimal") and abs(m2["gps_decimal"]["lat"] - 31.2304) < 1e-4)
    check("API-自动生成备份", os.path.exists(img_p + ".mforge.bak"))

    # ---------- 写入视频 ----------
    wv = post("/api/write", {
        "paths": [vid_p],
        "changes": {"©nam": "API标题", "©ART": "API作者"},
        "cover": None, "backup": True,
    })
    check("API-写入视频成功", wv["ok"], str(wv))
    mv2 = get_json("/api/meta", path=vid_p)
    check("API-视频标签已生效", mv2["ilst"].get("©nam", {}).get("value") == "API标题")

    # ---------- 视频封面 ----------
    im = Image.new("RGB", (400, 300), (10, 180, 90))
    b = io.BytesIO()
    im.save(b, "JPEG")
    import base64
    data_url = "data:image/jpeg;base64," + base64.b64encode(b.getvalue()).decode()
    cs = post("/api/cover/set", {"path": vid_p, "image": data_url, "backup": True})
    check("API-设置视频封面", cs["ok"], str(cs))
    raw = get_json("/api/cover", path=vid_p)
    check("API-提取视频封面", isinstance(raw, bytes) and raw[:2] == b"\xff\xd8")
    cr = post("/api/cover/remove", {"path": vid_p, "backup": True})
    check("API-移除视频封面", cr["ok"], str(cr))
    mv3 = get_json("/api/meta", path=vid_p)
    check("API-封面已消失", not mv3.get("preview"))
    check("API-移除封面后标题仍在", mv3["ilst"].get("©nam", {}).get("value") == "API标题")

    # ---------- 图片内嵌预览 ----------
    ps = post("/api/thumbnail/set", {"path": img_p, "image": data_url, "backup": True})
    check("API-设置图片内嵌预览", ps["ok"], str(ps))
    m3 = get_json("/api/meta", path=img_p)
    check("API-内嵌预览已生效", (m3.get("preview") or "").startswith("data:image"))

    # ---------- 隐私清理 ----------
    p2 = os.path.join(WORK, "sample_photo.png")
    post("/api/write", {"paths": [p2], "changes": {"相机型号": "LEAKED"},
                        "gps": {"lat": 22.5, "lon": 114.0}, "backup": False})
    sb = post("/api/scrub", {"paths": [p2], "preset": "strict", "backup": True})
    check("API-严格清理成功", sb["ok"], str(sb))
    m4 = get_json("/api/meta", path=p2)
    check("API-清理后无设备信息",
          not any("型号" in k for k in m4["exif"]), str(m4["exif"])[:120])
    check("API-清理后无GPS", m4.get("gps_decimal") is None and not m4["gps"])
    check("API-清理后图片仍可读", m4["basic"].get("宽度") == 640)

    sb2 = post("/api/scrub", {"paths": [p2], "preset": "geo", "backup": False})
    check("API-Geo档清理成功", sb2["ok"])

    # ---------- 缩略图接口 ----------
    # 必须在重命名之前测：重命名会改变文件名，原路径随之失效
    th = get_json("/api/thumb", path=img_p)
    check("API-缩略图", isinstance(th, bytes) and th[:2] == b"\xff\xd8")

    # ---------- 重命名 ----------
    pl = get_json("/api/rename/plan", folder=WORK,
                 template="{创建日期}_{序号2}_{文件名}", start=1, step=1)
    check("API-重命名预览", pl["ok"] and len(pl["plan"]) >= 3, str(pl)[:120])
    ok_rows = [r for r in pl["plan"] if r["ok"]]
    check("API-重命名方案无冲突", len(ok_rows) == len(pl["plan"]),
          str([r["reason"] for r in pl["plan"] if not r["ok"]]))

    exec_r = post("/api/rename/execute", {"plan": pl["plan"]})
    check("API-执行重命名", exec_r["ok"] and len(exec_r["renamed"]) >= 3, str(exec_r)[:200])
    renamed = [os.path.basename(r["to"]) for r in exec_r["renamed"]]
    check("API-重命名结果含序号", any("_01_" in n for n in renamed), str(renamed))

    # ---------- 备份恢复 ----------
    bak = img_p + ".mforge.bak"
    if os.path.exists(bak):
        rs = post("/api/restore", {"backup": bak, "original": img_p})
        check("API-恢复备份", rs["ok"], str(rs))

    # ---------- 错误处理 ----------
    err = post("/api/write", {"paths": [os.path.join(WORK, "不存在.jpg")],
                              "changes": {"软件": "x"}, "backup": False})
    check("API-不存在的文件报错而非崩溃", err["ok"] is False)
    st_bad, _ = get("/api/meta", path=os.path.join(WORK, "不存在.jpg"))
    check("API-读取不存在文件返回404", st_bad == 404, f"status={st_bad}")

except Exception as e:
    import traceback
    traceback.print_exc()
    FAIL.append(f"测试异常: {e}")
finally:
    httpd.shutdown()

print("\n" + "=" * 56)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for x in FAIL:
        print("  -", x)
print("=" * 56)
sys.exit(1 if FAIL else 0)
