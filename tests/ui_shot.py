"""
用 CDP 直接驱动 Edge 无头浏览器，模拟真实用户操作并截图。
不依赖 agent-browser 的 Chromium 下载，完全离线。

用法: python tests/ui_shot.py <端口> <演示目录>
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 9222
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")


def find_page(timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2) as r:
                pages = json.loads(r.read())
            for p in pages:
                if p.get("type") == "page" and "devtools" not in p.get("url", ""):
                    return p
        except Exception:
            pass
        time.sleep(0.5)
    return None


class CDP:
    def __init__(self, ws_url):
        import websocket  # 若不可用则退化为纯截图
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                return msg.get("result", {})

    def eval(self, expr):
        r = self.send("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        return r.get("result", {}).get("value")

    def shot(self, path):
        r = self.send("Page.captureScreenshot", format="png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "8971"
    folder = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else "demo")
    os.makedirs(OUT, exist_ok=True)

    profile = os.path.abspath(".edgeprofile_cdp")
    url = (f"http://127.0.0.1:{port}/?folder="
           + urllib.parse.quote(folder, safe=""))
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         f"--remote-debugging-port={PORT}", f"--user-data-dir={profile}",
         # CDP 会拒绝非预期 Origin 的 WebSocket 连接，本地调试需显式放行
         f"--remote-allow-origins=http://127.0.0.1:{PORT}",
         "--remote-allow-origins=*",
         "--window-size=1440,900", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        page = find_page()
        if not page:
            print("未能连接 CDP")
            return 1
        try:
            import websocket  # noqa: F401
            has_ws = True
        except ImportError:
            has_ws = False

        if not has_ws:
            print("需要 websocket-client 才能交互截图。")
            print("当前仅能确认服务可用。")
            return 2

        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        time.sleep(2.5)

        shots = []

        def shoot(name, note=""):
            p = os.path.abspath(os.path.join(OUT, name))
            cdp.shot(p)
            shots.append((name, note))
            print(f"  截图 {name}  {note}")

        print("① 首页")
        shoot("ui_1_home.png", "空状态")

        print("② 打开照片文件")
        cdp.eval("document.querySelector('.fitem').click()")
        time.sleep(1.6)
        shoot("ui_2_image.png", "图片 EXIF 编辑")

        print("③ GPS 页")
        cdp.eval("document.querySelector('[data-tab=gps]').click()")
        time.sleep(0.6)
        shoot("ui_3_gps.png", "GPS 定位")

        print("④ EXIF 详情页")
        cdp.eval("document.querySelector('[data-tab=exif]').click()")
        time.sleep(0.6)
        shoot("ui_4_exif.png", "EXIF 全量")

        print("⑤ 隐私清理页")
        cdp.eval("document.querySelector('[data-tab=privacy]').click()")
        time.sleep(0.6)
        shoot("ui_5_privacy.png", "隐私清理")

        print("⑥ 重命名页")
        cdp.eval("document.querySelector('[data-tab=rename]').click()")
        time.sleep(0.5)
        cdp.eval("document.querySelector('#btnRnPlan').click()")
        time.sleep(1.5)
        shoot("ui_6_rename.png", "重命名预览")

        print("⑦ 视频文件")
        cdp.eval("document.querySelector('[data-tab=basic]').click()")
        items = cdp.eval("JSON.stringify([...document.querySelectorAll('.fitem')].map(e=>e.dataset.path))")
        vp = [x for x in json.loads(items) if x.endswith(".mp4")]
        if vp:
            cdp.eval(f"openFile({json.dumps(vp[0])})")
            time.sleep(1.6)
            shoot("ui_7_video.png", "视频 ilst 标签")

        print(f"\n完成，共 {len(shots)} 张截图 → {os.path.abspath(OUT)}")
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    import urllib.parse
    sys.exit(main())
