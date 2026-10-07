# -*- coding: utf-8 -*-
"""导出 unified.py 的 write_video / read_video 段落与 privacy.scrub_video。"""
import sys

sys.stdout.reconfigure(encoding="utf-8")


def dump(path, lo, hi):
    src = open(path, encoding="utf-8").read().splitlines()
    print(f"===== {path} {lo}-{hi} =====")
    for i in range(lo - 1, min(hi, len(src))):
        print(f"{i + 1}: {src[i]}")


dump(r"G:\数据文件\work\2026-10-06-12-41-19\metaforge\core\unified.py", 195, 300)
