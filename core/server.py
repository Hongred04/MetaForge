"""
本地 HTTP 服务：给前端提供扫描 / 读取 / 写入 / 备份 / 重命名 API。
仅监听 127.0.0.1，使用随机端口，无任何外部通信。
"""

from __future__ import annotations

import json
import mimetypes
import os
import posixpath
import socket
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import images as _images
from . import privacy, quicktime, renamer, unified

def _resource_dir() -> str:
    """
    定位 web 资源目录。
    PyInstaller 打包后资源被解压到 sys._MEIPASS，源码目录相对路径不再适用。
    """
    import sys
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


WEB_DIR = os.path.join(_resource_dir(), "web")
# 缩略图缓存放在 exe 同级（用户可写），而不是打包资源目录（只读）
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(sys.executable))
                         if getattr(sys, "frozen", False)
                         else os.path.dirname(os.path.abspath(__file__)),
                         "_cache")

# 允许在界面上编辑的 EXIF 字段（安全白名单，避免误写结构型 tag）
EDITABLE_EXIF = [
    "相机制造商", "相机型号", "艺术家", "版权", "图片描述", "标题", "备注",
    "主题", "关键词", "修改日期", "拍摄时间", "数字化时间", "焦距",
    "镜头厂商", "镜头型号", "镜头序列号", "机身序列号", "用户备注",
]

_cache_lock = threading.Lock()

# ── 页面心跳自动退出 ─────────────────────────────────────────────
# 网页用 Web Worker 每 2 秒 ping 一次；页面/浏览器关闭或崩溃后心跳停止，
# 超时后退出进程。阈值 60 秒：足以覆盖浏览器对隐藏页的偶发节流，
# 也不会让"关网页退进程"显得迟钝。从未见过心跳（--no-browser 且无页面
# 打开、或脚本化调用）则永不自动退出。
PAGE_CLOSE_EXIT_SECONDS = 60.0
_page_state = {"seen": False, "last": 0.0}
_page_lock = threading.Lock()

# 进行中的"会改文件"的 POST 请求数（写入/清理/封面/重命名/恢复）。
# 用户在批量写盘途中关掉网页时，看门狗必须等写完才退，
# 否则 os._exit 会把正在重写的媒体文件截断在半截。
_busy_lock = threading.Lock()
_busy = 0


def _page_expired(now: float) -> bool:
    with _page_lock:
        if not _page_state["seen"]:
            return False
        return now - _page_state["last"] > PAGE_CLOSE_EXIT_SECONDS


def _page_watchdog() -> None:
    overdue = 0
    while True:
        time.sleep(2)
        if _page_expired(time.monotonic()):
            overdue += 1
            with _busy_lock:
                still_writing = _busy > 0
            if overdue >= 2 and not still_writing:   # 连续两拍超时才退，防睡眠唤醒竞态
                os._exit(0)           # 工作线程里只能硬退出；此刻无未落盘状态
        else:
            overdue = 0


def _cache_path(key: str) -> str:
    """
    缓存文件名。目录名截断后可能撞车（如 D:\\a\\x.jpg 与 D:\\b\\x.jpg），
    因此附加路径哈希，保证同名不同目录的缓存互不覆盖。
    """
    import hashlib
    h = hashlib.md5(key.encode("utf-8", "replace")).hexdigest()[:10]
    base = os.path.splitext(os.path.basename(key))[0]
    safe = "".join(c for c in base if c.isalnum() or c in "._-")[-40:]
    return os.path.join(CACHE_DIR, f"{safe}_{h}.bin")


class Handler(BaseHTTPRequestHandler):
    server_version = "MetaForge"

    def log_message(self, fmt, *args):  # 静音默认日志
        pass

    # -------------------------------------------------- 基础响应
    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _err(self, msg: str, code: int = 400):
        self._json({"ok": False, "error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    # -------------------------------------------------- 路由
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        q = urllib.parse.parse_qs(u.query)
        try:
            if path == "/api/scan":
                return self._api_scan(q)
            if path == "/api/meta":
                return self._api_meta(q)
            if path == "/api/cover":
                return self._api_cover(q)
            if path == "/api/thumb":
                return self._api_thumb(q)
            if path == "/api/ping":
                return self._api_ping()
            if path == "/api/fields":
                return self._json({
                    "exif_editable": EDITABLE_EXIF,
                    "video_editable": sorted(quicktime.WRITABLE_TEXT),
                    "gps_editable": sorted(quicktime.ILST_TAGS.get("©xyz", {}) and ["©xyz", "loci"]),
                    "rename_vars": sorted(renamer._VARS.keys()),
                    "presets": [
                        {"id": "standard", "name": "标准清理",
                         "desc": "去 GPS、设备型号、软件、序列号、作者与版权；保留尺寸与构图"},
                        {"id": "geo", "name": "仅去定位",
                         "desc": "只清除 GPS 经纬度与海拔，其余信息全部保留"},
                        {"id": "strict", "name": "完全归零",
                         "desc": "抹除除尺寸/分辨率外的全部元数据，适合对外发布"},
                    ],
                })
            if path == "/api/rename/plan":
                return self._api_rename_plan(q)
            return self._serve_static(path)
        except Exception as e:
            traceback.print_exc()
            return self._err(f"服务器错误: {e}", 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        global _busy
        try:
            b = self._body()
            with _busy_lock:
                _busy += 1
            try:
                if path == "/api/write":
                    return self._api_write(b)
                if path == "/api/scrub":
                    return self._api_scrub(b)
                if path == "/api/cover/set":
                    return self._api_cover_set(b)
                if path == "/api/cover/remove":
                    return self._api_cover_remove(b)
                if path == "/api/thumbnail/set":
                    return self._api_thumb_set(b)
                if path == "/api/rename/execute":
                    return self._api_rename_exec(b)
                if path == "/api/restore":
                    return self._api_restore(b)
                return self._err("未知接口", 404)
            finally:
                with _busy_lock:
                    _busy -= 1
        except Exception as e:
            traceback.print_exc()
            return self._err(f"服务器错误: {e}", 500)

    # -------------------------------------------------- 静态资源
    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        clean = posixpath.normpath(path).lstrip("/")
        full = os.path.join(WEB_DIR, clean.replace("/", os.sep))
        if not os.path.abspath(full).startswith(os.path.abspath(WEB_DIR)) or not os.path.isfile(full):
            return self._err("文件不存在", 404)
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or "javascript" in ctype or "json" in ctype:
            ctype += "; charset=utf-8"
        return self._send(200, body, ctype)

    # -------------------------------------------------- API 实现
    def _api_scan(self, q):
        folder = (q.get("folder") or [""])[0]
        recursive = (q.get("recursive") or ["0"])[0] == "1"
        if not folder or not os.path.isdir(folder):
            return self._err("目录不存在", 400)
        exts = {e.strip().lower().lstrip(".")
                for e in (q.get("exts") or ["jpg,jpeg,png,webp,tif,tiff,heic,heif,bmp,"
                                             "avif,mp4,mov,m4v,m4a,3gp"])[0].split(",")
                if e.strip()}
        # exts 已去掉前导点，比较时也必须用 lstrip(".")，否则永远匹配不上
        def _match(name: str) -> bool:
            return os.path.splitext(name)[1].lower().lstrip(".") in exts

        files = []
        try:
            if recursive:
                for root, _dirs, names in os.walk(folder):
                    for n in names:
                        if _match(n):
                            files.append(os.path.join(root, n))
            else:
                for n in os.listdir(folder):
                    full = os.path.join(folder, n)
                    if os.path.isfile(full) and _match(n):
                        files.append(full)
        except PermissionError:
            return self._err("没有权限访问该目录", 403)

        files.sort()
        rows = []
        for p in files[:2000]:
            kind = unified.kind_of(p)
            try:
                st = os.stat(p)
                size = _images._human(st.st_size)
                mtime = st.st_mtime
            except OSError:
                size, mtime = "-", 0
            rows.append({
                "path": p,
                "name": os.path.basename(p),
                "kind": kind,
                "size": size,
                "mtime": mtime,
                "has_gps": None,
                "has_cover": None,
            })
        return self._json({"ok": True, "folder": folder, "count": len(files),
                           "truncated": len(files) > 2000, "files": rows})

    def _read_any(self, path: str) -> dict:
        kind = unified.kind_of(path)
        if kind == "image":
            m = _images.read(path)
            m["kind"] = "image"
            m["xmp"] = m.get("xmp", "")
            return m
        if kind == "video":
            m = unified.read_video(path)
            m.setdefault("exif", {})
            m.setdefault("gps", {})
            return m
        raise ValueError("不支持的文件类型")

    def _api_meta(self, q):
        p = (q.get("path") or [""])[0]
        if not os.path.isfile(p):
            return self._err("文件不存在", 404)
        try:
            m = self._read_any(p)
        except Exception as e:
            return self._err(str(e), 500)
        m["ok"] = True
        # 图片封面：内嵌缩略图转 base64；视频：内嵌 covr 优先，无则现取画面帧
        if m.get("kind") == "image":
            m["preview"] = _image_preview_b64(p)
        else:
            m["preview"] = _video_preview_b64(p)
        return self._json(m)

    def _api_cover(self, q):
        p = (q.get("path") or [""])[0]
        try:
            got = unified.extract_cover(p)
        except Exception:
            got = None
        if not got:
            return self._err("该文件没有封面", 404)
        return self._send(200, got[0], f"image/{got[1]}")

    def _api_thumb(self, q):
        p = (q.get("path") or [""])[0]
        if not os.path.isfile(p):
            return self._err("文件不存在", 404)
        os.makedirs(CACHE_DIR, exist_ok=True)
        cp = _cache_path(p)
        with _cache_lock:
            if not _cache_fresh(cp, p):
                # 图片走 PIL；视频用内嵌封面或 Shell 画面帧（PIL 打不开 MP4）
                if unified.kind_of(p) == "video":
                    got = None
                    try:
                        got = unified.extract_cover(p)
                    except Exception:
                        got = None
                    if got:
                        with open(cp, "wb") as f:
                            f.write(got[0])
                    elif not unified.make_video_frame_preview(p, cp):
                        return self._err("无法生成预览", 500)
                elif not unified.make_preview_image(p, cp):
                    return self._err("无法生成预览", 500)
            with open(cp, "rb") as f:
                data = f.read()
        return self._send(200, data, "image/jpeg")

    def _api_ping(self):
        with _page_lock:
            _page_state["seen"] = True
            _page_state["last"] = time.monotonic()
        return self._json({"ok": True})

    def _api_write(self, b):
        paths = b.get("paths") or []
        changes = b.get("changes") or {}
        gps = b.get("gps")
        xmp = b.get("xmp")
        do_backup = b.get("backup", True)
        results = []
        for p in paths:
            entry = {"path": p}
            try:
                if do_backup:
                    entry["backup"] = unified.backup(p)
                kind = unified.kind_of(p)
                if kind == "image":
                    w = _images.write(p, changes=changes, gps=gps, xmp=xmp)
                elif kind == "video":
                    clear_gps = gps is None and b.get("clear_gps")
                    drop = ["©xyz", "loci"] if clear_gps else []
                    w = unified.write_video(p, changes=changes, gps=gps,
                                            drop=drop, xmp=xmp,
                                            drop_keys=["*location*", "*Location*"] if clear_gps else None)
                else:
                    w = [f"不支持的类型: {p}"]
                entry["warnings"] = w
                entry["ok"] = not any("失败" in x for x in w)
                results.append(entry)
            except Exception as e:
                entry["ok"] = False
                entry["error"] = str(e)
                results.append(entry)
        return self._json({"ok": all(r.get("ok") for r in results), "results": results})

    def _api_scrub(self, b):
        paths = b.get("paths") or []
        preset = b.get("preset", "standard")
        do_backup = b.get("backup", True)
        results = []
        for p in paths:
            entry = {"path": p}
            try:
                if do_backup:
                    entry["backup"] = unified.backup(p)
                w = privacy.scrub(p, preset)
                entry["warnings"] = w
                entry["ok"] = True
            except Exception as e:
                entry["ok"] = False
                entry["error"] = str(e)
            results.append(entry)
        return self._json({"ok": all(r.get("ok") for r in results), "results": results})

    def _api_cover_set(self, b):
        p = b.get("path")
        img = b.get("image")          # base64 data URL
        if not p or not img or not os.path.isfile(p):
            return self._err("参数不完整", 400)
        try:
            blob = _parse_data_url(img)
        except Exception as e:
            return self._err(f"图片解析失败: {e}", 400)
        if unified.sniff_image_mime(blob) is None:
            return self._err("这不是可识别的图片（支持 JPEG / PNG / WebP / GIF / BMP / TIFF）", 400)
        backup = unified.backup(p) if b.get("backup", True) else None
        try:
            kind = unified.kind_of(p)
            if kind == "video":
                w = unified.write_video(p, cover=blob)
            elif kind == "image":
                w = unified.write_embedded_preview_image_blob(p, blob)
            else:
                return self._err("不支持的类型", 400)
            return self._json({"ok": True, "warnings": w, "backup": backup})
        except Exception as e:
            return self._err(str(e), 500)

    def _api_cover_remove(self, b):
        p = b.get("path")
        if not p or not os.path.isfile(p):
            return self._err("参数不完整", 400)
        backup = unified.backup(p) if b.get("backup", True) else None
        try:
            kind = unified.kind_of(p)
            if kind == "video":
                w = unified.remove_video_cover(p)
            elif kind == "image":
                w = unified.remove_image_preview(p)
            else:
                return self._err("不支持的类型", 400)
            return self._json({"ok": True, "warnings": w, "backup": backup})
        except Exception as e:
            return self._err(str(e), 500)

    def _api_thumb_set(self, b):
        p = b.get("path")
        img = b.get("image")
        if not p or not img or not os.path.isfile(p):
            return self._err("参数不完整", 400)
        try:
            blob = _parse_data_url(img)
        except Exception as e:
            return self._err(f"图片解析失败: {e}", 400)
        backup = unified.backup(p) if b.get("backup", True) else None
        try:
            w = unified.write_embedded_preview_image_blob(p, blob)
            return self._json({"ok": True, "warnings": w, "backup": backup})
        except Exception as e:
            return self._err(str(e), 500)

    def _api_rename_plan(self, q):
        folder = (q.get("folder") or [""])[0]
        tpl = (q.get("template") or ["{创建日期}_{序号2}"])[0]
        start = int((q.get("start") or ["1"])[0])
        step = int((q.get("step") or ["1"])[0])
        sel = (q.get("paths") or [""])[0]
        paths = [p for p in sel.split("|") if p] if sel else []
        if not paths:
            exts = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".m4v"}
            if os.path.isdir(folder):
                paths = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                         if os.path.isfile(os.path.join(folder, n))
                         and os.path.splitext(n)[1].lower() in exts]
        metas = {}
        for p in paths[:400]:
            try:
                metas[p] = self._read_any(p)
            except Exception:
                metas[p] = {}
        rows = renamer.plan(paths, tpl, start, step, metas)
        return self._json({"ok": True, "plan": rows})

    def _api_rename_exec(self, b):
        rows = b.get("plan") or []
        rows = [{"path": r.get("new_path") and r.get("path"), "new_path": r["new_path"],
                 "ok": r.get("ok", True)} for r in rows if r.get("ok")]
        if not rows:
            return self._err("没有可执行的重命名项", 400)
        res = renamer.execute(rows)
        return self._json({"ok": not res["failed"], **res})

    def _api_restore(self, b):
        backup = b.get("backup")
        if not backup or not os.path.isfile(backup):
            return self._err("备份文件不存在", 404)
        orig = b.get("original")
        try:
            import shutil
            shutil.copy2(backup, orig)
            return self._json({"ok": True, "restored": orig})
        except Exception as e:
            return self._err(str(e), 500)


# -------------------------------------------------- 辅助
def _b64(data: bytes) -> str:
    import base64
    return base64.b64encode(data).decode("ascii")


def _parse_data_url(url: str) -> bytes:
    import base64
    if url.startswith("data:"):
        url = url.split(",", 1)[1]
    return base64.b64decode(url)


def _cache_fresh(cache_file: str, src: str) -> bool:
    """缓存比源文件新才算有效，否则文件改过了需要重新生成。"""
    try:
        return os.path.exists(cache_file) and os.path.getmtime(cache_file) >= os.path.getmtime(src)
    except OSError:
        return False


def _image_preview_b64(path: str) -> str | None:
    """从图片里抽一块可视预览：内嵌缩略图优先，否则现生成缩略图。"""
    try:
        raw = open(path, "rb").read()
        seg = _images._extract_exif_segment(raw)
        if seg:
            import piexif
            d = piexif.load(seg)
            if d.get("thumbnail"):
                return "data:image/jpeg;base64," + _b64(d["thumbnail"])
    except Exception:
        pass
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = _cache_path(path)
    with _cache_lock:
        if not _cache_fresh(cp, path) and not unified.make_preview_image(path, cp):
            return None
        with open(cp, "rb") as f:
            return "data:image/jpeg;base64," + _b64(f.read())


def _video_preview_b64(path: str) -> str | None:
    """视频预览：内嵌 covr 优先；没有则用 Windows Shell 取画面帧。都失败返回 None。"""
    try:
        got = unified.extract_cover(path)
    except Exception:
        got = None
    if got:
        return "data:image/%s;base64,%s" % (got[1], _b64(got[0]))
    os.makedirs(CACHE_DIR, exist_ok=True)
    cp = _cache_path(path)
    with _cache_lock:
        if not _cache_fresh(cp, path):
            if not unified.make_video_frame_preview(path, cp):
                return None
        try:
            with open(cp, "rb") as f:
                return "data:image/jpeg;base64," + _b64(f.read())
        except OSError:
            return None


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(port: int | None = None, on_ready=None, watch_page: bool = True):
    os.makedirs(CACHE_DIR, exist_ok=True)
    port = port or find_free_port()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    if on_ready:
        on_ready(port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    if watch_page:
        threading.Thread(target=_page_watchdog, daemon=True).start()
    return httpd
