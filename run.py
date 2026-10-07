"""
MetaForge 启动入口。

用法:
  python run.py                  启动并自动打开浏览器
  python run.py --port 8899      指定端口
  python run.py --no-browser     不打开浏览器
  python run.py --folder "D:\照片"  启动时预填目录
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser

# 源码运行时把项目根加入路径；PyInstaller 打包后 core 已被收集进 exe，
# 此时再改 sys.path 反而可能引入同名模块冲突，故跳过。
if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core import server  # noqa: E402

BANNER = r"""
  __  __         _
 |  \/  | __ ___ | |_  ___ _____      __
 | |\/| |/ _` \ \| __|/ _ \ \ / \ \ /\ / /
 | |  | | (_| |> | |_|  __/ |\ \ \ \  / /
 |_|  |_|\__,_|_/ \__|\___|_| \_\_\_\_\/ /
              图片 · 视频 元数据工坊
"""


def wait_port(port: int, timeout: float = 8.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        with socket.socket() as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.12)
    return False


def _safe_print(*args):
    """打包成 windowed 程序后 stdout 为 None，直接 print 会抛异常。"""
    try:
        print(*args)
    except Exception:
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="MetaForge 元数据工坊")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--folder", default=None, help="启动时预填的文件夹")
    args = ap.parse_args()

    port = args.port or server.find_free_port()
    url = f"http://127.0.0.1:{port}/"

    httpd = server.serve(port)
    if not wait_port(port):
        _safe_print("服务启动失败：端口未就绪")
        return 1

    _safe_print(BANNER)
    _safe_print(f"  服务地址  {url}")
    _safe_print("  关闭此窗口即退出程序\n")

    if args.folder:
        # 通过 URL 参数把目录传给前端，省去手输
        url += "?folder=" + urllib.parse.quote(args.folder)

    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.5), webbrowser.open(url)),
                         daemon=True).start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        _safe_print("\n已退出")
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
