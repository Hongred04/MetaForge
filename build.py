"""
打包脚本：生成单文件 Windows 可执行程序。

用法:
  python build.py            正常打包
  python build.py --clean    先清理旧的构建产物

产物: dist/MetaForge/MetaForge.exe
运行时不需要 Python 环境，双击即可启动。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
NAME = "MetaForge"

REQUIRED = [
    ("core/atoms.py", "core"),
    ("core/quicktime.py", "core"),
    ("core/images.py", "core"),
    ("core/unified.py", "core"),
    ("core/renamer.py", "core"),
    ("core/privacy.py", "core"),
    ("core/server.py", "core"),
    ("web/index.html", "web"),
    ("web/style.css", "web"),
    ("web/app.js", "web"),
]

# PyInstaller 未收集到的隐藏导入
HIDDEN = ["PIL._tkinter_finder", "piexif"]


def preflight() -> bool:
    print("── 检查依赖 ──────────────────────────────")
    ok = True
    for rel, _ in REQUIRED:
        full = os.path.join(ROOT, rel)
        if os.path.isfile(full):
            print(f"  ✓ {rel}")
        else:
            print(f"  ✗ {rel} 缺失")
            ok = False

    try:
        import PIL, piexif  # noqa: F401
        print(f"  ✓ Pillow {PIL.__version__}")
        print(f"  ✓ piexif {piexif.VERSION}")
    except ImportError as e:
        print(f"  ✗ 缺少依赖: {e}")
        print("    请先运行: pip install pillow piexif")
        ok = False
    return ok


def clean():
    for d in ("build", "dist", f"{NAME}.spec"):
        p = os.path.join(ROOT, d)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
            print(f"  已删除 {d}/")
        elif os.path.isfile(p):
            os.remove(p)
            print(f"  已删除 {d}")
    # 清理 __pycache__
    for root, dirs, _ in os.walk(ROOT):
        for d in dirs:
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def build():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed",                       # 不弹控制台窗口
        "--name", NAME,
        "--onedir",                         # 目录模式启动更快
        "--add-data", f"{os.path.join(ROOT, 'web')}{os.pathsep}web",
        "--hidden-import", "piexif",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "numpy",
        "--exclude-module", "PyQt5",
        "--exclude-module", "PySide2",
    ]
    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    cmd.append(os.path.join(ROOT, "run.py"))

    print("\n── 执行打包 ──────────────────────────────")
    print("  " + " ".join(cmd[:6]) + " …\n")
    r = subprocess.run(cmd, cwd=ROOT)
    return r.returncode == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    args = ap.parse_args()

    if args.clean:
        clean()

    if not preflight():
        print("\n依赖检查未通过，已中止。")
        return 1

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("\n缺少 PyInstaller，请先运行: pip install pyinstaller")
        return 1

    if not build():
        print("\n打包失败。")
        return 1

    exe = os.path.join(ROOT, "dist", NAME, f"{NAME}.exe")
    print("\n" + "=" * 52)
    if os.path.isfile(exe):
        size = os.path.getsize(exe) / 1024 / 1024
        print(f"  打包完成：{exe}")
        print(f"  可执行文件大小：{size:.1f} MB")
        print(f"\n  双击 dist\\{NAME}\\{NAME}.exe 即可运行")
        print("  （首次启动会在同目录生成 _cache 文件夹存放缩略图缓存）")
    else:
        print("  未找到产物，请检查上方日志。")
    print("=" * 52)
    return 0


if __name__ == "__main__":
    sys.exit(main())
