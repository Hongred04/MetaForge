"""
images.py —— 图片元数据读写。

支持格式: JPEG / PNG / WebP / TIFF / HEIC(读取为主)
元数据: EXIF (IFD0/Exif/GPS/Interop) + XMP (APP1 / PNG iTXt) + IPTC

写法说明:
  * EXIF 走 piexif (纯 Python, GPS 往返稳定)
  * XMP 走手写段注入，JPEG 用 APP1，PNG 用 iTXt —— 这样不依赖 exiftool
  * 优先"原地改字节"而非重编码，避免画质损失和体积变化
"""

from __future__ import annotations

import io
import struct
from typing import Any

import piexif
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # 允许大图（航拍/全景）

SUPPORTED = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".tif", ".tiff",
             ".heic", ".heif", ".bmp", ".avif"}

XMP_NS = "http://ns.adobe.com/xap/1.0/\x00"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"

# ---- EXIF 标签展示映射 --------------------------------------------------
EXIF_DISPLAY = {
    (0, piexif.ImageIFD.Make): "相机制造商",
    (0, piexif.ImageIFD.Model): "相机型号",
    (0, piexif.ImageIFD.Software): "软件",
    (0, piexif.ImageIFD.Artist): "艺术家",
    (0, piexif.ImageIFD.Copyright): "版权",
    (0, piexif.ImageIFD.ImageDescription): "图片描述",
    (0, piexif.ImageIFD.DateTime): "修改日期",
    (0, piexif.ImageIFD.XPTitle): "标题(XP)",
    (0, piexif.ImageIFD.XPComment): "备注(XP)",
    (0, piexif.ImageIFD.XPSubject): "主题(XP)",
    (0, piexif.ImageIFD.XPKeywords): "关键词(XP)",
    (0, piexif.ImageIFD.Orientation): "方向",
    (0, piexif.ImageIFD.Rating): "评分",
    (0, piexif.ImageIFD.DocumentName): "文档名",
    (1, piexif.ExifIFD.ExposureTime): "曝光时间",
    (1, piexif.ExifIFD.FNumber): "光圈 F 值",
    (1, piexif.ExifIFD.ISOSpeedRatings): "ISO",
    (1, piexif.ExifIFD.DateTimeOriginal): "拍摄时间",
    (1, piexif.ExifIFD.DateTimeDigitized): "数字化时间",
    (1, piexif.ExifIFD.FocalLength): "焦距",
    (1, piexif.ExifIFD.FocalLengthIn35mmFilm): "35mm 等效焦距",
    (1, piexif.ExifIFD.LensMake): "镜头厂商",
    (1, piexif.ExifIFD.LensModel): "镜头型号",
    (1, piexif.ExifIFD.LensSerialNumber): "镜头序列号",
    (1, piexif.ExifIFD.BodySerialNumber): "机身序列号",
    (1, piexif.ExifIFD.PixelXDimension): "宽度",
    (1, piexif.ExifIFD.PixelYDimension): "高度",
    (1, piexif.ExifIFD.WhiteBalance): "白平衡",
    (1, piexif.ExifIFD.ExposureBiasValue): "曝光补偿",
    (1, piexif.ExifIFD.ExposureProgram): "曝光程序",
    (1, piexif.ExifIFD.MeteringMode): "测光模式",
    (1, piexif.ExifIFD.Flash): "闪光灯",
    (1, piexif.ExifIFD.UserComment): "用户备注",
}

GPS_DISPLAY = {
    piexif.GPSIFD.GPSLatitudeRef: "纬度参考",
    piexif.GPSIFD.GPSLatitude: "纬度",
    piexif.GPSIFD.GPSLongitudeRef: "经度参考",
    piexif.GPSIFD.GPSLongitude: "经度",
    piexif.GPSIFD.GPSAltitudeRef: "海拔参考",
    piexif.GPSIFD.GPSAltitude: "海拔",
    piexif.GPSIFD.GPSDateStamp: "GPS 日期",
    piexif.GPSIFD.GPSProcessingMethod: "定位方式",
    piexif.GPSIFD.GPSSpeedRef: "速度参考",
    piexif.GPSIFD.GPSSpeed: "速度",
    piexif.GPSIFD.GPSImgDirectionRef: "方位参考",
    piexif.GPSIFD.GPSImgDirection: "方位角",
}


# ---------------------------------------------------------------- 工具
def _to_bytes(v: Any) -> bytes:
    if isinstance(v, bytes):
        return v
    return str(v).encode("utf-8", "ignore")


def _rat_to_float(r: Any) -> float | None:
    try:
        if isinstance(r, tuple) and len(r) == 2:
            return r[0] / r[1] if r[1] else None
        if isinstance(r, (list, tuple)) and len(r) and isinstance(r[0], tuple):
            d = r[0]
            return d[0] / d[1] if d[1] else None
    except Exception:
        return None
    return None


def dms_to_degrees(dms: Any, ref: Any) -> float | None:
    """((31,1),(14,1),(0,1)) + b'N' -> 31.233333"""
    try:
        if isinstance(dms, (list, tuple)):
            parts = []
            for item in dms:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    parts.append(item[0] / item[1] if item[1] else 0)
            if not parts:
                return None
        elif isinstance(dms, int):
            parts = [dms / 3600000.0, 0, 0]
        else:
            return None
        deg = parts[0] + parts[1] / 60.0 + (parts[2] / 3600.0 if len(parts) > 2 else 0)
        if isinstance(ref, bytes):
            ref = ref.decode("ascii", "ignore")
        if str(ref).upper().startswith("S") or str(ref).upper().startswith("W"):
            deg = -deg
        return deg
    except Exception:
        return None


def degrees_to_dms(deg: float) -> tuple[tuple, bytes]:
    """31.233333 -> (((31,1),(14,1),(2000,100)), b'N')"""
    hemi = b"N" if deg >= 0 else b"S"
    a = abs(deg)
    d = int(a)
    m_f = (a - d) * 60
    m = int(m_f)
    s = round((m_f - m) * 60, 4)
    return ((d, 1), (m, 1), (int(s * 10000), 10000)), hemi


def load_exif_dict(data: bytes) -> dict:
    """
    从任意输入解析出 piexif 字典。
    兼容三种输入：完整 JPEG 文件字节 / 带 Exif\0\0 前缀的段 / TIFF 头。
    """
    empty = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}, "1st": {}, "thumbnail": None}
    if not data:
        return empty
    # PIL 等库有时会重复附加 "Exif\0\0" 前缀，这里统一剥到只剩一层
    while data[:6] == b"Exif\x00\x00" and data[6:12] == b"Exif\x00\x00":
        data = data[6:]
    try:
        if data[:2] == b"\xff\xd8":            # 完整 JPEG → 抽 Exif 段
            seg = _extract_exif_segment(data)
            if not seg:
                return empty
            return piexif.load(seg)
        if data[:6] == b"Exif\x00\x00":
            return piexif.load(data)
        if data[:8] == b"\x89PNG\r\n\x1a\n":   # PNG → 找 eXIf chunk
            i = 8
            while i + 8 <= len(data):
                ln = struct.unpack_from(">I", data, i)[0]
                ctype = data[i + 4:i + 8]
                body = data[i + 8:i + 8 + ln]
                if ctype == b"eXIf":
                    if body[:6] == b"Exif\x00\x00":
                        return piexif.load(body)
                    return piexif.load(b"Exif\x00\x00" + body)
                if ctype == b"IEND":
                    break
                i += 12 + ln
            return empty
        if data[:4] in (b"II\x2a\x00", b"MM\x00\x2a"):  # 裸 TIFF
            return piexif.load(data)
        return empty
    except Exception:
        return empty


def _extract_exif_segment(jpeg: bytes) -> bytes | None:
    """从 JPEG 中抽出 APP1 Exif 段（含 Exif\\0\\0 前缀）。"""
    i = 2
    n = len(jpeg)
    while i + 4 <= n:
        if jpeg[i] != 0xFF:
            break
        marker = jpeg[i + 1]
        if marker == 0xDA:
            break
        if marker == 0xD8 or (0xD0 <= marker <= 0xD7) or marker == 0x01:
            i += 2
            continue
        seg_len = struct.unpack_from(">H", jpeg, i + 2)[0]
        payload = jpeg[i + 4:i + 2 + seg_len]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            return payload
        i += 2 + seg_len
    return None


# ---------------------------------------------------------------- 读取
def read(path: str) -> dict:
    result: dict = {
        "kind": "image",
        "basic": {},
        "exif": {},
        "gps": {},
        "xmp": "",
        "has_exif": False,
        "warnings": [],
    }
    try:
        with Image.open(path) as im:
            result["basic"] = {
                "格式": im.format or "?",
                "尺寸": f"{im.width} × {im.height}",
                "宽度": im.width,
                "高度": im.height,
                "色彩模式": im.mode,
            }
            exif_bytes = im.info.get("exif")
            if not exif_bytes and im.getexif():
                exif_bytes = b"Exif\x00\x00" + im.getexif().tobytes()
            if exif_bytes:
                result["has_exif"] = True
                _parse_exif_into(result, exif_bytes)
            xmp = im.info.get("xmp")
            if xmp:
                result["xmp"] = xmp.decode("utf-8", "ignore") if isinstance(xmp, bytes) else str(xmp)
    except Exception as e:
        result["warnings"].append(f"读取图片失败: {e}")
        return result

    if not result["xmp"]:
        result["xmp"] = _extract_xmp_raw(path)
    if not result["basic"].get("文件大小"):
        import os
        result["basic"]["文件大小"] = _human(os.path.getsize(path))
    return result


def _parse_exif_into(result: dict, exif_bytes: bytes) -> None:
    d = load_exif_dict(exif_bytes)
    for ifd, prefix in (("0th", "0th"), ("Exif", "Exif"), ("Interop", "Interop")):
        for tag, val in (d.get(ifd) or {}).items():
            # 跳过指向子 IFD 的偏移指针（34665→Exif, 34853→GPS, 40965→Interop），
            # 它们对用户无意义，内容已在对应分组中展示
            if tag in (0x8769, 0x8825, 0xA005):
                continue
            name = EXIF_DISPLAY.get((0 if ifd == "0th" else 1, tag)) or _fallback_name(tag)
            result["exif"][f"{name} [{prefix}:{tag}]"] = _fmt(val)
    for tag, val in (d.get("GPS") or {}).items():
        name = GPS_DISPLAY.get(tag) or _fallback_name(tag)
        result["gps"][f"{name} [GPS:{tag}]"] = _fmt(val)

    gps = d.get("GPS") or {}
    if piexif.GPSIFD.GPSLatitude in gps:
        lat = dms_to_degrees(gps.get(piexif.GPSIFD.GPSLatitude), gps.get(piexif.GPSIFD.GPSLatitudeRef))
        lon = dms_to_degrees(gps.get(piexif.GPSIFD.GPSLongitude), gps.get(piexif.GPSIFD.GPSLongitudeRef))
        if lat is not None and lon is not None:
            alt = _rat_to_float(gps.get(piexif.GPSIFD.GPSAltitude))
            if alt is not None and gps.get(piexif.GPSIFD.GPSAltitudeRef) == 1:
                alt = -alt
            result["gps_decimal"] = {"lat": round(lat, 7), "lon": round(lon, 7),
                                     "alt": round(alt, 2) if alt is not None else None}


_TAG_FALLBACK = {
    0x010E: "ImageDescription", 0x0131: "Software", 0x0132: "ModifyDate",
    0x013B: "Artist", 0x8298: "Copyright", 0x9C9B: "Title(XP)", 0x9C9C: "Comment(XP)",
    0x9C9D: "Author(XP)", 0x9C9E: "Keywords(XP)", 0x9C9F: "Subject(XP)",
    0x9003: "DateTimeOriginal", 0x9004: "CreateDate", 0x9291: "SubSecCreateDate",
    0xA002: "PixelXDimension", 0xA003: "PixelYDimension", 0xA434: "LensModel",
    0xA431: "BodySerialNumber", 0xA435: "LensSerialNumber",
}


def _fallback_name(tag: int) -> str:
    return _TAG_FALLBACK.get(tag, f"Tag {tag}")


def _fmt(v: Any) -> str:
    if isinstance(v, bytes):
        t = v.decode("utf-8", "ignore").rstrip("\x00").strip()
        return t if t else f"<{len(v)} 字节二进制>"
    if isinstance(v, tuple) and len(v) == 2 and all(isinstance(x, int) for x in v):
        return f"{v[0]}/{v[1]}" if v[1] != 1 else str(v[0])
    return str(v)


def _human(n: int) -> str:
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def _extract_xmp_raw(path: str) -> str:
    """直接从文件字节里捞 XMP（Adobe XMP 标准是 APP1 段 / PNG iTXt）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4 * 1024 * 1024)
        i = head.find(b"<x:xmpmeta")
        if i == -1:
            i = head.find(b"<?xpacket begin")
        if i == -1:
            return ""
        j = head.find(b"</x:xmpmeta>", i)
        if j == -1:
            j = head.find(b"<?xpacket end", i)
            j = len(head) if j == -1 else j
        return head[i:j + 12].decode("utf-8", "ignore")
    except Exception:
        return ""


# ---------------------------------------------------------------- 写入
# EXIF 标签名 → piexif tag（写入用）
_WRITE_MAP = {
    "相机制造商": ("0th", piexif.ImageIFD.Make),
    "相机型号": ("0th", piexif.ImageIFD.Model),
    "软件": ("0th", piexif.ImageIFD.Software),
    "艺术家": ("0th", piexif.ImageIFD.Artist),
    "作者": ("0th", piexif.ImageIFD.Artist),
    "版权": ("0th", piexif.ImageIFD.Copyright),
    "图片描述": ("0th", piexif.ImageIFD.ImageDescription),
    "标题": ("0th", piexif.ImageIFD.XPTitle),
    "备注": ("0th", piexif.ImageIFD.XPComment),
    "主题": ("0th", piexif.ImageIFD.XPSubject),
    "关键词": ("0th", piexif.ImageIFD.XPKeywords),
    "修改日期": ("0th", piexif.ImageIFD.DateTime),
    "拍摄时间": ("Exif", piexif.ExifIFD.DateTimeOriginal),
    "数字化时间": ("Exif", piexif.ExifIFD.DateTimeDigitized),
    "焦距": ("Exif", piexif.ExifIFD.FocalLength),
    "镜头厂商": ("Exif", piexif.ExifIFD.LensMake),
    "镜头型号": ("Exif", piexif.ExifIFD.LensModel),
    "镜头序列号": ("Exif", piexif.ExifIFD.LensSerialNumber),
    "机身序列号": ("Exif", piexif.ExifIFD.BodySerialNumber),
    "用户备注": ("Exif", piexif.ExifIFD.UserComment),
}


def _build_exif(src: bytes, changes: dict, gps: dict | None,
                drop_exif: bool = False, drop_gps: bool = False) -> bytes:
    """基于源文件 EXIF 生成新的 EXIF 字节。"""
    if drop_exif and drop_gps:
        return b""
    d = load_exif_dict(src)

    if drop_exif:
        d["0th"] = {}
        d["Exif"] = {}
        d["Interop"] = {}
        d["thumbnail"] = None
    if drop_gps:
        d["GPS"] = {}

    for name, value in (changes or {}).items():
        if value is None:
            continue
        mapped = _WRITE_MAP.get(name)
        if mapped:
            ifd_name, tag = mapped
            d.setdefault(ifd_name, {})
            if name in ("焦距",):
                d[ifd_name][tag] = (float(value), 1) if _is_num(value) else _to_bytes(value)
            else:
                d[ifd_name][tag] = _to_bytes(value)
        elif ":" in name:
            # 形如 "GPSLatitudeRef [GPS:1]"
            try:
                ifd_name, tag_s = name.split("[")[-1].rstrip("]").split(":")
                tag = int(tag_s)
                d.setdefault(ifd_name, {})
                d[ifd_name][tag] = _to_bytes(value)
            except Exception:
                pass

    if gps and gps.get("lat") is not None and gps.get("lon") is not None:
        lat_dms, lat_ref = degrees_to_dms(float(gps["lat"]))
        lon_dms, lon_ref = degrees_to_dms(float(gps["lon"]))
        g = dict(d.get("GPS") or {})
        g[piexif.GPSIFD.GPSLatitudeRef] = lat_ref
        g[piexif.GPSIFD.GPSLatitude] = lat_dms
        g[piexif.GPSIFD.GPSLongitudeRef] = lon_ref
        g[piexif.GPSIFD.GPSLongitude] = lon_dms
        if gps.get("alt") is not None:
            alt = float(gps["alt"])
            g[piexif.GPSIFD.GPSAltitudeRef] = 1 if alt < 0 else 0
            g[piexif.GPSIFD.GPSAltitude] = (int(abs(alt) * 100), 100)
        d["GPS"] = g
        from datetime import datetime
        d["GPS"][piexif.GPSIFD.GPSDateStamp] = datetime.now().strftime("%Y:%m:%d").encode()

    if not any(d.get(k) for k in ("0th", "Exif", "GPS", "Interop", "1st")) and not d.get("thumbnail"):
        return b""
    try:
        # 统一返回裸 TIFF 数据（不含 "Exif\0\0" 前缀），由各格式写入层自行加前缀，
        # 避免 piexif.dump 已带头 + 注入时再拼一次造成双前缀。
        raw = piexif.dump(d)
        if raw.startswith(b"Exif\x00\x00"):
            raw = raw[6:]
        return raw
    except Exception:
        return b""


def _is_num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _jpeg_set_segments(data: bytes, *, exif: bytes | None = None,
                       xmp: str | None = None, drop: tuple[str, ...] = ()) -> bytes:
    """
    重写 JPEG 的 APP1(Exif) / APP1(XMP) / APP13(IPTC) 段，其余字节原样保留。
    """
    if not data.startswith(b"\xff\xd8"):
        return data
    out = bytearray(b"\xff\xd8")
    i = 2
    inserted_exif = exif is None
    inserted_xmp = xmp is None
    inserted_iptc = "iptc" in drop

    while i < len(data):
        if data[i] != 0xFF:
            out += data[i:]
            break
        marker = data[i + 1]
        if marker == 0xDA:  # SOS —— 图像数据开始，停止
            out += data[i:]
            break
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            out += data[i:i + 2]
            i += 2
            continue
        seg_len = struct.unpack_from(">H", data, i + 2)[0]
        segment = data[i:i + 2 + seg_len]
        payload = segment[4:]

        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            if not inserted_exif and exif:
                out += _jpeg_segment(0xE1, b"Exif\x00\x00" + exif)
                inserted_exif = True
            # 值为空则丢弃原段
        elif marker == 0xE1 and payload.startswith(XMP_HEADER):
            if not inserted_xmp:
                if xmp and xmp.strip():
                    out += _jpeg_segment(0xE1, XMP_HEADER + xmp.encode("utf-8"))
                inserted_xmp = True
        elif marker == 0xED:
            if not inserted_iptc:
                inserted_iptc = True
                # IPTC 被丢弃（隐私清除）
            else:
                out += segment
        else:
            out += segment
        i += 2 + seg_len

    if not inserted_exif and exif:
        out = bytearray(b"\xff\xd8") + _jpeg_segment(0xE1, b"Exif\x00\x00" + exif) + bytes(out)[2:]
    if not inserted_xmp and xmp and xmp.strip():
        newseg = _jpeg_segment(0xE1, XMP_HEADER + xmp.encode("utf-8"))
        out = bytearray(b"\xff\xd8") + newseg + bytes(out)[2:]
    return bytes(out)


def _jpeg_segment(marker: int, payload: bytes) -> bytes:
    return struct.pack(">BBH", 0xFF, marker, len(payload) + 2) + payload


def _png_chunk(ctype: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + ctype + payload
            + struct.pack(">I", _crc(ctype + payload)))


def _png_insert_chunks(data: bytes, new_chunks: list[bytes], before_first: tuple[bytes, ...] = (b"IDAT",)) -> bytes:
    """
    重建 PNG chunk 流：跳过指定类型的旧块，插入新块。
    新块必须放在 IDAT 之前 —— 放在 IEND 之后虽然部分读取器能容忍，
    但严格解析器和部分播放器会直接忽略。
    """
    out = bytearray(data[:8])
    inserted = not before_first
    i = 8
    n = len(data)
    while i + 8 <= n:
        ln = struct.unpack_from(">I", data, i)[0]
        ctype = data[i + 4:i + 8]
        end = i + 12 + ln
        if ctype == b"IEND":
            if not inserted:
                for c in new_chunks:
                    out += c
            out += data[i:end]
            i = end
            break
        if not inserted and ctype in before_first:
            for c in new_chunks:
                out += c
            inserted = True
        if not any(c[4:8] == ctype for c in new_chunks):
            out += data[i:end]
        i = end
    return bytes(out)


def _png_set_exif(data: bytes, exif: bytes) -> bytes:
    if not exif:
        return data
    # eXIf chunk 内容以 "Exif\0\0" 开头（PNG 规范 1.5+）
    chunk = _png_chunk(b"eXIf", b"Exif\x00\x00" + exif)
    return _png_insert_chunks(data, [chunk])


def _png_set_xmp(data: bytes, xmp: str) -> bytes:
    if not (xmp or "").strip():
        return _png_remove_chunk(data, b"iTXt")
    payload = b"XML:com.adobe.xmp\x00" + b"\x00\x00\x00\x00\x00" + xmp.encode("utf-8")
    return _png_insert_chunks(data, [_png_chunk(b"iTXt", payload)])


def _png_remove_chunk(data: bytes, ctype: bytes) -> bytes:
    out = bytearray(data[:8])
    i, n = 8, len(data)
    while i + 8 <= n:
        ln = struct.unpack_from(">I", data, i)[0]
        t = data[i + 4:i + 8]
        end = i + 12 + ln
        if t == b"IEND":
            out += data[i:end]
            break
        if t != ctype:
            out += data[i:end]
        i = end
    return bytes(out)


def _crc(data: bytes) -> int:
    import zlib
    return zlib.crc32(data) & 0xFFFFFFFF


def write(path: str, *, changes: dict | None = None, gps: dict | None = None,
          xmp: str | None = None, drop_exif: bool = False, drop_gps: bool = False,
          drop_xmp: bool = False, drop_iptc: bool = False,
          thumbnail: bytes | None = None) -> list[str]:
    """把改动写回图片。返回警告列表。"""
    warnings: list[str] = []
    with open(path, "rb") as f:
        data = f.read()

    ext = path.lower().rsplit(".", 1)[-1]
    exif = _build_exif(data, changes or {}, gps, drop_exif, drop_gps)
    xmp_out = None if drop_xmp else xmp

    if ext in ("jpg", "jpeg", "jpe"):
        new = _jpeg_set_segments(data, exif=exif, xmp=xmp_out,
                                 drop=("iptc",) if drop_iptc else ())
    elif ext == "png":
        new = data
        if exif:
            new = _png_set_exif(new, exif)
        if xmp_out is not None or drop_xmp:
            new = _png_set_xmp(new, xmp_out or "")
    elif ext == "webp":
        new = data
        try:
            im = Image.open(io.BytesIO(data))
            kw = {}
            if exif:
                kw["exif"] = exif
            if xmp_out and xmp_out.strip():
                kw["xmp"] = xmp_out.encode("utf-8")
            buf = io.BytesIO()
            im.save(buf, "WEBP", quality=100, **kw)
            new = buf.getvalue()
            warnings.append("WebP 需重编码保存（格式限制）")
        except Exception as e:
            warnings.append(f"WebP 写入失败，仅 EXIF 未更新: {e}")
    elif ext in ("tif", "tiff"):
        try:
            im = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            save_kw = {"tiffinfo": exif} if exif else {}
            im.save(buf, "TIFF", **save_kw)
            new = buf.getvalue()
        except Exception as e:
            warnings.append(f"TIFF 写入失败: {e}")
            new = data
    else:
        warnings.append(f"{ext.upper()} 暂不支持写入 EXIF")
        new = data

    if thumbnail:
        new = _apply_embedded_preview(new, thumbnail)

    if new != data:
        with open(path, "wb") as f:
            f.write(new)
    return warnings


def _apply_embedded_preview(data: bytes, jpeg_bytes: bytes) -> bytes:
    """把 JPEG 预览图写进 EXIF IFD1（JPEG/PNG 支持）。"""
    try:
        d = load_exif_dict(data) if b"Exif\x00\x00" in data[:4096] else None
        if not d:
            return data
        im = Image.open(io.BytesIO(jpeg_bytes))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
        d["thumbnail"] = buf.getvalue()
        exif = piexif.dump(d)
        if exif.startswith(b"Exif\x00\x00"):
            exif = exif[6:]
        return _jpeg_set_segments(data, exif=exif)
    except Exception:
        return data
