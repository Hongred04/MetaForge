"""
批量重命名引擎：按元数据模板生成新文件名。

模板变量（大小写不敏感，需用 {变量名} 包裹）：
  {文件名} {扩展名} {序号} {序号2} {序号3} {创建日期} {拍摄日期} {创建时间}
  {时:分:秒} {年} {月} {日} {相机} {制造商} {镜头} {作者} {标题} {描述}
  {版权} {经度} {纬度} {海拔} {宽度} {高度}
"""

from __future__ import annotations

import os
import re
from datetime import datetime

from . import images as _images
from . import unified

_VARS = {
    "文件名": lambda d, ctx: os.path.splitext(ctx["path"])[0],
    "扩展名": lambda d, ctx: ctx["path"].rsplit(".", 1)[-1].lower() if "." in ctx["path"] else "",
    "序号": lambda d, ctx: str(ctx["index"]),
    "序号2": lambda d, ctx: f"{ctx['index']:02d}",
    "序号3": lambda d, ctx: f"{ctx['index']:03d}",
    "创建日期": lambda d, ctx: ctx["date"].strftime("%Y-%m-%d") if ctx["date"] else "",
    "拍摄日期": lambda d, ctx: ctx["date"].strftime("%Y-%m-%d") if ctx["date"] else "",
    "创建时间": lambda d, ctx: ctx["date"].strftime("%H-%M-%S") if ctx["date"] else "",
    "年": lambda d, ctx: f"{ctx['date'].year:04d}" if ctx["date"] else "",
    "月": lambda d, ctx: f"{ctx['date'].month:02d}" if ctx["date"] else "",
    "日": lambda d, ctx: f"{ctx['date'].day:02d}" if ctx["date"] else "",
    "时": lambda d, ctx: f"{ctx['date'].hour:02d}" if ctx["date"] else "",
    "分": lambda d, ctx: f"{ctx['date'].minute:02d}" if ctx["date"] else "",
    "秒": lambda d, ctx: f"{ctx['date'].second:02d}" if ctx["date"] else "",
    "相机": lambda d, ctx: d.get("_camera", ""),
    "制造商": lambda d, ctx: d.get("_make", ""),
    "镜头": lambda d, ctx: d.get("_lens", ""),
    "作者": lambda d, ctx: d.get("_artist", ""),
    "标题": lambda d, ctx: d.get("_title", ""),
    "描述": lambda d, ctx: d.get("_desc", ""),
    "版权": lambda d, ctx: d.get("_copyright", ""),
    "经度": lambda d, ctx: _fmt_num(d.get("_lon")),
    "纬度": lambda d, ctx: _fmt_num(d.get("_lat")),
    "海拔": lambda d, ctx: _fmt_num(d.get("_alt"), 0),
    "宽度": lambda d, ctx: str(d.get("basic", {}).get("宽度", "")),
    "高度": lambda d, ctx: str(d.get("basic", {}).get("高度", "")),
}

_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_TOKEN = re.compile(r"\{([^{}]+)\}")


def _fmt_num(v, nd: int = 6) -> str:
    if v is None:
        return ""
    try:
        return f"{float(v):.{nd}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return ""


def _extract(meta: dict) -> dict:
    """从统一元数据结构中抽出重命名需要的扁平字段。"""
    out: dict = {}
    ex = meta.get("exif") or {}
    for key, val in ex.items():
        name = key.split(" [")[0]
        if name == "相机型号":
            out["_camera"] = val
        elif name == "相机制造商":
            out["_make"] = val
        elif name == "镜头型号":
            out["_lens"] = val
        elif name in ("艺术家", "作者"):
            out["_artist"] = val
        elif name == "图片描述":
            out["_desc"] = val
        elif name == "版权":
            out["_copyright"] = val
        elif name == "标题(XP)":
            out["_title"] = val

    il = meta.get("ilst") or {}
    for key in ("©nam",):
        if key in il:
            out.setdefault("_title", il[key].get("value", ""))
    for key in ("©ART",):
        if key in il:
            out.setdefault("_artist", il[key].get("value", ""))
    for key in ("cprt",):
        if key in il:
            out.setdefault("_copyright", il[key].get("value", ""))

    g = meta.get("gps_decimal") or {}
    if g:
        out["_lat"] = g.get("lat")
        out["_lon"] = g.get("lon")
        out["_alt"] = g.get("alt")
    return out


_DATE_PATTERNS = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y:%m:%d", "%Y年%m月%d日 %H:%M:%S",
]


def _pick_date(meta: dict, fallback: os.stat_result | None) -> datetime | None:
    ex = meta.get("exif") or {}
    for key in ("拍摄时间 [Exif:36867]", "拍摄时间 [Exif:36868]", "数字化时间 [Exif:36868]"):
        if key in ex:
            dt = _parse_date(str(ex[key]))
            if dt:
                return dt
    for key in ex:
        if "拍摄时间" in key or "数字化时间" in key:
            dt = _parse_date(str(ex[key]))
            if dt:
                return dt
    il = meta.get("ilst") or {}
    if "©day" in il:
        dt = _parse_date(str(il["©day"].get("value", "")))
        if dt:
            return dt
    if fallback:
        return datetime.fromtimestamp(fallback.st_mtime)
    return None


def _parse_date(s: str) -> datetime | None:
    s = s.strip().replace("\x00", "")
    for p in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, p)
        except ValueError:
            continue
    return None


def render(template: str, meta: dict, path: str, index: int, fallback_stat=None) -> str:
    flat = _extract(meta)
    ctx = {"path": path, "index": index, "date": _pick_date(meta, fallback_stat)}

    def repl(m: re.Match) -> str:
        name = m.group(1).strip()
        fn = _VARS.get(name)
        if fn is None:
            return ""            # 未知变量留空，避免把 {xxx} 写进文件名
        try:
            v = fn(flat, ctx)
        except Exception:
            v = ""
        return _INVALID.sub("", str(v)).strip()

    out = _TOKEN.sub(repl, template)
    out = _INVALID.sub("", out).strip(" .")
    return out[:180] or "untitled"


def plan(paths: list[str], template: str, start: int = 1, step: int = 1,
         metas: dict | None = None) -> list[dict]:
    """
    生成重命名预览计划（含冲突检测），不实际执行。
    返回 [{path, new_name, new_path, ok, reason}]
    """
    metas = metas or {}
    plan_rows: list[dict] = []
    taken: dict[str, str] = {}

    for i, p in enumerate(paths):
        idx = start + i * step
        meta = metas.get(p) or _read_meta(p)
        try:
            st = os.stat(p)
        except OSError:
            st = None
        new_name = render(template, meta, p, idx, st)
        ext = os.path.splitext(p)[1]
        new_name = new_name + ext
        new_path = os.path.join(os.path.dirname(p), new_name)

        ok, reason = True, ""
        if not new_name.strip():
            ok, reason = False, "模板渲染结果为空"
        elif new_path.lower() in taken:
            ok, reason = False, f"与「{taken[new_path.lower()]}」重名"
        elif os.path.exists(new_path) and os.path.normcase(new_path) != os.path.normcase(p):
            ok, reason = False, "目标文件已存在"
        elif new_path == p:
            ok, reason = False, "与原名相同"

        if ok:
            taken[new_path.lower()] = os.path.basename(p)
        plan_rows.append({"path": p, "new_name": new_name, "new_path": new_path,
                          "ok": ok, "reason": reason,
                          "date": (meta and "") or ""})
    return plan_rows


def _read_meta(path: str) -> dict:
    try:
        k = unified.kind_of(path)
        if k == "image":
            m = _images.read(path)
            m["kind"] = "image"
            return m
        if k == "video":
            return unified.read_video(path)
    except Exception:
        pass
    return {}


def execute(plan_rows: list[dict]) -> dict:
    """执行重命名计划。分两阶段以正确处理 a→b、b→c 这类互换。"""
    done, failed = [], []
    staged = []
    tmp_paths: dict[str, str] = {}

    for r in plan_rows:
        if not r.get("ok"):
            failed.append({"path": r["path"], "error": r.get("reason", "不可执行")})
            continue
        try:
            tmp = r["path"] + ".mforge_rn"
            os.replace(r["path"], tmp)
            tmp_paths[r["path"]] = tmp
            staged.append(r)
        except OSError as e:
            failed.append({"path": r["path"], "error": str(e)})

    for r in staged:
        try:
            os.replace(tmp_paths[r["path"]], r["new_path"])
            done.append({"from": r["path"], "to": r["new_path"]})
        except OSError as e:
            try:
                os.replace(tmp_paths[r["path"]], r["path"])
            except OSError:
                pass
            failed.append({"path": r["path"], "error": str(e)})

    return {"renamed": done, "failed": failed}
