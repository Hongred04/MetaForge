"""统一元数据读写入口：按文件类型分派到图片 / 视频引擎。"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass

from . import atoms, images, quicktime
from .atoms import parse, serialize, walk, collect  # noqa: F401  (对外暴露)

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".3gp", ".3g2", ".m4a", ".avi", ".webm"}
IMAGE_EXT = images.SUPPORTED


def kind_of(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return "unknown"


# ---------------------------------------------------------------- 视频
def _load_moov(path: str) -> tuple[list, bytes | None, int, list[str]]:
    """
    定位并解析 moov（兼容任意文件大小与 moov 位置）。

    返回 (moov 的 box 树, ftyp 的 brand 字节或 None, 文件总长, 顶层 box 名列表)。
    找不到可解析的 moov 时 box 树为空列表。以前「头尾各读 48MB 拼接」
    的做法在拼接边界会把 mdat 撑成吞掉 moov 的巨 box，moov 在尾部的
    手机视频全部读取失败——已改为流式索引 + 按需读取 moov。
    """
    tops, total = atoms.scan_top_level(path)
    brand = None
    moov_span = None
    for btype, start, size in tops:
        if btype == b"ftyp" and size >= 16:
            with open(path, "rb") as f:
                f.seek(start + 8)
                brand = f.read(4)
        elif btype == b"moov" and size <= 512 * 1024 * 1024:
            moov_span = (start, size)
    if moov_span:
        with open(path, "rb") as f:
            f.seek(moov_span[0])
            blob = f.read(moov_span[1])
        boxes = atoms.parse(blob)
        top_names = [t.decode("latin-1", "ignore") for t, _s, _z in tops[:8]]
        return boxes, brand, total, top_names
    return [], brand, total, [t.decode("latin-1", "ignore") for t, _s, _z in tops[:8]]


def read_video(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    result: dict = {"kind": "video", "basic": {}, "ilst": {}, "keys": {}, "gps": {},
                    "xmp": "", "warnings": [], "tracks": []}

    if ext in (".avi", ".webm"):
        try:
            total = os.path.getsize(path)
        except OSError:
            total = 0
        result["warnings"].append("AVI/WebM 使用 RIFF 容器，当前仅支持读取容器概要")
        result["basic"] = {"容器": "RIFF", "文件大小": images._human(total)}
        return result

    boxes, brand, total, top_names = _load_moov(path)

    result["basic"] = {
        "容器": f"ISO-BMFF ({brand.decode('latin-1', 'ignore')})" if brand else "未知",
        "文件大小": images._human(total),
        "box 顶层结构": ", ".join(top_names),
    }
    if not boxes:
        result["warnings"].append("未找到 moov box，可能不是 ISO-BMFF 视频或文件已损坏")
        return result

    # 时长 / 分辨率
    # 时长：mvhd = version/flags(4) + [v0: creation(4)+modification(4) | v1: 各 8 字节]
    #                  + timescale(4) + duration(4 或 8)
    mvhd = walk(boxes, [b"moov", b"mvhd"])
    if mvhd and len(mvhd.payload) >= 20:
        p = bytes(mvhd.payload)
        ver = p[0]
        if ver == 1:
            base = 4 + 8 + 8          # 跳过 creation/modification
            dur_size = 8
        else:
            base = 4 + 4 + 4
            dur_size = 4
        if len(p) >= base + 4 + dur_size:
            timescale = int.from_bytes(p[base:base + 4], "big")
            dur = int.from_bytes(p[base + 4:base + 4 + dur_size], "big")
            if timescale:
                secs = dur / timescale
                result["basic"]["时长"] = f"{int(secs // 60)}:{int(secs % 60):02d} ({secs:.2f}s)"

    for trak in collect(boxes, b"trak"):
        tkhd = walk(trak.children, [b"tkhd"])
        label = "?"
        try:
            if tkhd and len(tkhd.payload) >= 84:
                w = int.from_bytes(tkhd.payload[-8:-4], "big", signed=False) >> 16
                h = int.from_bytes(tkhd.payload[-4:], "big", signed=False) >> 16
                if w and h:
                    label = f"{w}×{h}"
        except Exception:
            pass
        handler = walk(trak.children, [b"mdia", b"hdlr"])
        htype = "?"
        if handler and len(handler.payload) >= 12:
            htype = handler.payload[8:12].decode("latin-1", "ignore")
        result["tracks"].append({"类型": htype, "分辨率": label})

    meta = quicktime.read_ilst(boxes)
    for tag, val in meta["ilst"].items():
        info = (quicktime.ILST_TAGS.get(tag) or quicktime.KEY_LABELS.get(tag)
                or {"label": tag, "group": "其他", "type": "text"})
        if info.get("type") == "binary":
            result["ilst"][tag] = {**info, "value": f"<{val['size']} 字节 {val['mime']}>"}
        else:
            result["ilst"][tag] = {**info, "value": val["value"]}
    result["keys"] = meta["keys"]
    result["xmp"] = meta["xmp"]
    # GPS：©xyz（iTunes 体系）→ Keys 的 location.ISO6709（iPhone/安卓）→ loci
    if "©xyz" in meta["ilst"]:
        gps = quicktime.parse_gps_text(meta["ilst"]["©xyz"]["value"])
        if gps:
            result["gps_decimal"] = gps
    elif "com.apple.quicktime.location.ISO6709" in meta["ilst"]:
        gps = quicktime.parse_gps_text(
            meta["ilst"]["com.apple.quicktime.location.ISO6709"]["value"])
        if gps:
            result["gps_decimal"] = gps
    elif meta.get("loci"):
        result["gps_decimal"] = meta["loci"]
    # 创建时间：优先 ©day / Keys creationdate（人写、含时区），
    # 否则取 mvhd creation_time（相机写入的 UTC）
    ctime = ""
    if "©day" in meta["ilst"]:
        ctime = meta["ilst"]["©day"]["value"]
    elif "com.apple.quicktime.creationdate" in meta["ilst"]:
        ctime = meta["ilst"]["com.apple.quicktime.creationdate"]["value"]
    elif mvhd:
        p = bytes(mvhd.payload)
        if p and p[0] == 1 and len(p) >= 12:
            secs = int.from_bytes(p[4:12], "big")
        elif len(p) >= 8:
            secs = int.from_bytes(p[4:8], "big")
        else:
            secs = 0
        if secs:
            ctime = quicktime.mp4_seconds_to_text(secs)
    result["creation_time"] = ctime
    result["has_exif"] = False
    return result


def sniff_image_mime(raw: bytes) -> str | None:
    """按 magic bytes 判断图片真实类型，认不出返回 None。

    不能信任 data box 里写的类型标记：很多工具（GPAC 等）会往 covr 里塞
    占位文本却标成 jpeg，或把非 JPEG 数据标成 implicit。必须看真实字节。
    """
    if raw[:2] == b"\xff\xd8":
        return "jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw[:2] == b"BM":
        return "bmp"
    if raw[:4] == b"II*\x00" or raw[:4] == b"MM\x00*":
        return "tiff"
    return None


def normalize_cover(raw: bytes) -> bytes:
    """把任意可解码图片转成 JPEG。

    covr 里塞非 JPEG/PNG（WebP/HEIC/BMP…）时，多数播放器不认，
    表现为"设了封面但看不到"。统一转 JPEG 最保险。
    """
    if raw[:2] == b"\xff\xd8":
        return raw
    try:
        from PIL import Image
        import io as _io
        im = Image.open(_io.BytesIO(raw))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = _io.BytesIO()
        im.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:
        # 解不出来就原样返回，交给上层校验并报错
        return raw


def extract_cover(path: str) -> tuple[bytes, str] | None:
    """取出封面原始字节 (jpeg/png/webp/...)。非图片内容返回 None。"""
    boxes, _brand, _total, _names = _load_moov(path)
    if not boxes:
        return None
    ilst = walk(boxes, [b"moov", b"udta", b"meta", b"ilst"]) or walk(boxes, [b"moov", b"meta", b"ilst"])
    if not ilst:
        for b in atoms.collect(boxes, b"ilst"):
            ilst = b
            break
    if not ilst:
        return None
    for item in ilst.children:
        if item.type != b"covr":
            continue
        vals = []
        if item.children:
            for d in item.children:
                if d.type == b"data":
                    vals.append(quicktime._decode_data(bytes(d.payload))[1])
        elif not item.is_container and item.payload:
            vals.append(quicktime._decode_data(bytes(item.payload))[1])
        for val in vals:
            if not val:
                continue
            mime = sniff_image_mime(val)
            if mime is None:
                # 认不出就不是真封面（占位文本/损坏数据），继续找下一个 covr
                continue
            return val, mime
    return None


def write_video(path: str, *, changes: dict | None = None, gps: dict | None = None,
                cover: bytes | None = None, xmp: str | None = None,
                drop: list[str] | None = None,
                drop_keys: list[str] | None = None,
                drop_keys_all: bool = False,
                creation_time: str | None = None) -> list[str]:
    """流式写入视频元数据：内存占用与文件大小无关（数 GB 视频也不会卡死）。"""
    warnings: list[str] = []
    if creation_time is not None and quicktime.parse_time_text(creation_time) is None:
        warnings.append(f"创建时间格式无法识别，已跳过：{creation_time}")
        creation_time = None
    tmp = path + ".mforge.tmp"
    try:
        total, in_place = quicktime.write_ilst_file(
            path, tmp, changes or {}, cover=cover, gps=gps, drop=drop or [],
            xmp=xmp, drop_keys=drop_keys, drop_keys_all=drop_keys_all,
            creation_time=creation_time)
    except Exception as e:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return [f"写入失败: {e}"]
    if total < 16:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return ["写入失败：结果异常，文件可能损坏，已放弃"]
    if not in_place:
        os.replace(tmp, path)
    if (not cover and not changes and not gps and xmp is None and not drop
            and not drop_keys and not drop_keys_all and creation_time is None):
        warnings.append("没有需要写入的改动")
    return warnings


# ---------------------------------------------------------------- 备份
def backup(path: str, root: str | None = None) -> str:
    """生成 .bak 备份，返回备份路径。"""
    base = path + ".mforge.bak"
    i = 1
    while os.path.exists(base):
        base = f"{path}.mforge.{i}.bak"
        i += 1
    shutil.copy2(path, base)
    return base


def remove_video_cover(path: str) -> list[str]:
    """移除视频封面图（流式，大文件安全）。"""
    return write_video(path, drop=["covr"])


def remove_image_preview(path: str) -> list[str]:
    """移除图片内嵌预览图（EXIF IFD1 thumbnail）。"""
    try:
        data = open(path, "rb").read()
        d = images.load_exif_dict(data)
        if not d.get("thumbnail"):
            return ["该图片没有内嵌预览图"]
        d["thumbnail"] = None
        exif = piexif_dump(d)
        new = images._jpeg_set_segments(data, exif=exif)
        with open(path, "wb") as f:
            f.write(new)
        return []
    except Exception as e:
        return [f"移除预览失败: {e}"]


def piexif_dump(d: dict) -> bytes:
    import piexif
    raw = piexif.dump(d)
    return raw[6:] if raw.startswith(b"Exif\x00\x00") else raw


def make_preview_image(path: str, dest: str, max_side: int = 480) -> bool:
    """生成缩略图预览。失败时返回 False（原因可通过 PREVIEW_DEBUG 打印）。"""
    try:
        from PIL import Image, ImageOps
        im = Image.open(path)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_side, max_side))
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        im.save(dest, "JPEG", quality=80)
        return True
    except Exception as e:
        if os.environ.get("METAFORGE_DEBUG"):
            import traceback
            print(f"[preview] {path} 失败: {e}", file=__import__("sys").stderr)
            traceback.print_exc()
        return False


def make_video_frame_preview(path: str, dest: str, max_side: int = 480) -> bool:
    """
    为没有内嵌封面的视频生成画面缩略图（Windows Shell 的视频缩略图）。

    纯 ctypes 调用 IShellItemImageFactory::GetImage，无需 ffmpeg。
    部分精简系统/沙箱环境会拒绝该 COM 接口——失败一律返回 False，
    由上层降级为占位图，绝不能影响主流程。
    """
    if os.name != "nt":
        return False
    try:
        return _shell_video_thumbnail(path, dest, max_side)
    except Exception:
        return False


def _shell_video_thumbnail(path: str, dest: str, max_side: int) -> bool:
    import ctypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                    ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    IID_IMGF = GUID(0xBCC18B98, 0x4C57, 0x4AE9,
                    (0x91, 0x96, 0xE1, 0x75, 0x2B, 0x24, 0x66, 0xB4))

    def vtbl_fn(comptr, slot, restype, *argtypes):
        vtbl = ctypes.cast(ctypes.cast(comptr, ctypes.POINTER(ctypes.c_void_p)).contents,
                           ctypes.POINTER(ctypes.c_void_p))
        return ctypes.WINFUNCTYPE(restype, *argtypes)(ctypes.c_void_p(vtbl[slot]).value)

    def release(comptr):
        if comptr:
            vtbl_fn(comptr, 2, ctypes.c_ulong, ctypes.c_void_p)(comptr)

    ole32 = ctypes.WinDLL("ole32")
    shell32 = ctypes.WinDLL("shell32")
    gdi32 = ctypes.WinDLL("gdi32")
    user32 = ctypes.WinDLL("user32")
    ole32.CoInitialize(None)
    factory = ctypes.c_void_p()
    try:
        create = shell32.SHCreateItemFromParsingName
        create.restype = ctypes.c_long
        create.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
                           ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        if create(path, None, ctypes.byref(IID_IMGF), ctypes.byref(factory)) != 0 or not factory:
            return False
        # IShellItemImageFactory::GetImage（vtable 槽 3）
        SIIGBF_THUMBNAILONLY = 0x02
        SIIGBF_BIGGERSIZEOK = 0x01
        get_image = vtbl_fn(factory, 3, ctypes.c_long, ctypes.c_void_p, ctypes.c_int,
                            ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p))
        hbitmap = ctypes.c_void_p()
        hr = get_image(factory, max_side, max_side,
                       SIIGBF_THUMBNAILONLY | SIIGBF_BIGGERSIZEOK, ctypes.byref(hbitmap))
        if hr != 0 or not hbitmap:
            return False
        try:
            from PIL import Image

            class BITMAP(ctypes.Structure):
                _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long),
                            ("bmHeight", ctypes.c_long), ("bmWidthBytes", ctypes.c_long),
                            ("bmPlanes", ctypes.c_ushort), ("bmBitsPixel", ctypes.c_ushort),
                            ("bmBits", ctypes.c_void_p)]

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [("biSize", ctypes.c_ulong), ("biWidth", ctypes.c_long),
                            ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_ushort),
                            ("biBitCount", ctypes.c_ushort), ("biCompression", ctypes.c_ulong),
                            ("biSizeImage", ctypes.c_ulong), ("biXPelsPerMeter", ctypes.c_long),
                            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_ulong),
                            ("biClrImportant", ctypes.c_ulong)]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_ulong * 3)]

            bm = BITMAP()
            if not gdi32.GetObjectW(hbitmap, ctypes.sizeof(BITMAP), ctypes.byref(bm)):
                return False
            w, h = bm.bmWidth, abs(bm.bmHeight)
            if w <= 0 or h <= 0 or w * h > 64_000_000:
                return False
            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h          # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0      # BI_RGB
            buf = ctypes.create_string_buffer(w * h * 4)
            hdc = user32.GetDC(None)
            try:
                if not gdi32.GetDIBits(hdc, hbitmap, 0, h, buf, ctypes.byref(bmi), 0):
                    return False
            finally:
                user32.ReleaseDC(None, hdc)
            im = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
            im.save(dest, "JPEG", quality=80)
            return True
        finally:
            gdi32.DeleteObject(hbitmap)
    finally:
        release(factory)
        ole32.CoUninitialize()


def write_embedded_preview_image(path: str, img_path: str) -> list[str]:
    """把一张图片设为 JPEG 内嵌缩略图（EXIF IFD1）。"""
    try:
        with open(img_path, "rb") as f:
            blob = f.read()
        return images.write(path, thumbnail=blob)
    except Exception as e:
        return [f"内嵌预览写入失败: {e}"]


def write_embedded_preview_image_blob(path: str, blob: bytes) -> list[str]:
    """同上，但直接接收图片字节（网页端上传的 data URL）。"""
    try:
        return images.write(path, thumbnail=blob)
    except Exception as e:
        return [f"内嵌预览写入失败: {e}"]
