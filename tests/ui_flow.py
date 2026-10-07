"""
UI 全流程验证：通过 CDP 驱动浏览器完成真实操作，
覆盖「改 EXIF → 保存 → 重新读取确认生效」与「隐私清理」两条关键链路。

这是服务层测试覆盖不到的部分 —— 它验证的是前端逻辑与后端的实际联动。

用法: python tests/ui_flow.py <端口> <工作目录>
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
CDP_PORT = 9223

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'OK  ' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail and not cond else ""))


class CDP:
    def __init__(self, ws_url):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                r = msg.get("result", {})
                if "exceptionDetails" in r:
                    raise RuntimeError(r["exceptionDetails"].get("text", "JS error"))
                return r

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=True)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError(res.get("description", "JS error"))
        return res.get("value")


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else "8971"
    work = os.path.abspath(sys.argv[2] if len(sys.argv) > 2 else "demo")

    # 每次用干净副本，避免污染演示数据
    work = os.path.abspath("tests/_uiflow")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    for n in ("sample_photo.jpg", "sample_video.mp4"):
        src = os.path.join("demo", n)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(work, n))

    url = (f"http://127.0.0.1:{port}/?folder=" + urllib.parse.quote(work, safe=""))
    proc = subprocess.Popen(
        [EDGE, "--headless=new", "--disable-gpu", f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={os.path.abspath('.edgeprofile_flow')}",
         f"--remote-allow-origins=*", "--window-size=1440,900", url],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        page = None
        t0 = time.time()
        while time.time() - t0 < 25:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2) as r:
                    for p in json.loads(r.read()):
                        if p.get("type") == "page" and "devtools" not in p.get("url", ""):
                            page = p
                            break
                if page:
                    break
            except Exception:
                pass
            time.sleep(0.4)
        if not page:
            print("无法连接浏览器")
            return 1

        cdp = CDP(page["webSocketDebuggerUrl"])
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        time.sleep(3)

        # 关闭弹窗（confirm 在无头模式下默认返回 true，但保险起见覆写）
        cdp.js("window.confirm = () => true; window.alert = () => {};")

        # ---------- 1. 列表已加载 ----------
        n = cdp.js("document.querySelectorAll('.fitem').length")
        check("UI-列表加载", n == 2, f"items={n}")

        # ---------- 2. 打开图片 ----------
        cdp.js("document.querySelector('.fitem').click()")
        time.sleep(1.8)
        check("UI-编辑器显示", cdp.js("!document.querySelector('#editor').hidden"))

        # ---------- 3. EXIF 字段已回填 ----------
        maker = cdp.js("(document.querySelector('input[data-exif=\"相机制造商\"]')||{}).value")
        check("UI-EXIF回填", maker == "Canon", f"value={maker!r}")

        # ---------- 4. 改值并触发脏标记 ----------
        cdp.js("""
          (() => {
            const i = document.querySelector('input[data-exif="相机型号"]');
            i.value = 'UI-TEST-999';
            i.dispatchEvent(new Event('input', {bubbles:true}));
          })()
        """)
        time.sleep(0.4)
        dirty = cdp.js("document.querySelectorAll('.dirty').length")
        check("UI-脏标记", dirty > 0, f"dirty={dirty}")
        foot = cdp.js("document.querySelector('#footInfo').textContent")
        check("UI-底栏提示", "待保存" in foot, f"text={foot!r}")

        # ---------- 5. 保存 ----------
        cdp.js("document.querySelector('#btnSave').click()")
        time.sleep(2.5)

        # 后端直接验证落盘结果
        sys.path.insert(0, os.path.abspath("."))
        from core import images as _img
        jpg = os.path.join(work, "sample_photo.jpg")
        d = _img.read(jpg)
        model = d["exif"].get("相机型号 [0th:272]")
        check("UI-保存写回磁盘", model == "UI-TEST-999", f"model={model!r}")
        check("UI-保存生成备份", os.path.exists(jpg + ".mforge.bak"))

        # ---------- 6. 脏标记已清除 ----------
        time.sleep(1.0)
        dirty2 = cdp.js("document.querySelectorAll('.dirty').length")
        check("UI-保存后重置脏标记", dirty2 == 0, f"dirty={dirty2}")

        # ---------- 7. 视频标签编辑 ----------
        cdp.js(f"openFile({json.dumps(os.path.join(work, 'sample_video.mp4'))})")
        time.sleep(1.8)
        vtag = cdp.js("(document.querySelector('input[data-vtag=\"©nam\"]')||{}).value")
        check("UI-视频标签回填", vtag == "城市夜景延时", f"value={vtag!r}")

        cdp.js("""
          (() => {
            const i = document.querySelector('input[data-vtag="©nam"]');
            i.value = 'UI改的标题';
            i.dispatchEvent(new Event('input', {bubbles:true}));
          })()
        """)
        cdp.js("document.querySelector('#btnSave').click()")
        time.sleep(2.5)

        sys.path.insert(0, os.path.abspath("."))
        from core import unified
        vm = unified.read_video(os.path.join(work, "sample_video.mp4"))
        check("UI-视频保存生效",
              vm["ilst"].get("©nam", {}).get("value") == "UI改的标题",
              str(vm["ilst"].get("©nam")))
        check("UI-视频保存后其他标签保留",
              vm["ilst"].get("©ART", {}).get("value") == "MetaForge",
              str(vm["ilst"]))

        # ---------- 8. 隐私清理 ----------
        cdp.js(f"openFile({json.dumps(jpg)})")
        time.sleep(1.6)
        cdp.js("document.querySelector('[data-tab=privacy]').click()")
        time.sleep(0.4)
        cdp.js("""
          document.querySelector('.preset[data-p="strict"]').click();
        """)
        time.sleep(0.3)
        cdp.js("document.querySelector('#btnScrub').click()")
        time.sleep(2.8)

        d2 = _img.read(jpg)
        check("UI-隐私清理生效",
              not any("型号" in k for k in d2["exif"]), str(d2["exif"])[:120])
        check("UI-清理后图片完好", d2["basic"].get("宽度") == 640)

        # ---------- 9. 重命名预览 ----------
        cdp.js("document.querySelector('[data-tab=rename]').click()")
        time.sleep(0.3)
        cdp.js("document.querySelector('#rnTemplate').value='UI{序号2}'")
        cdp.js("document.querySelector('#btnRnPlan').click()")
        time.sleep(2.0)
        rows = cdp.js("document.querySelectorAll('.rn-item').length")
        check("UI-重命名预览渲染", rows >= 1, f"rows={rows}")

        # ---------- 10. 页签切换无报错 ----------
        tabs = ["basic", "gps", "exif", "raw", "privacy", "rename"]
        for t in tabs:
            cdp.js(f"document.querySelector('[data-tab={t}]').click()")
            time.sleep(0.15)
        visible = cdp.js("document.querySelectorAll('.pane.active').length")
        check("UI-页签切换正常", visible == 1, f"active={visible}")

    except Exception as e:
        import traceback
        traceback.print_exc()
        FAIL.append(f"异常: {e}")
    finally:
        proc.terminate()

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
    for f in FAIL:
        print("  -", f)
    print("=" * 56)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
