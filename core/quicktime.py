"""
quicktime.py —— MP4/MOV/M4V 的元数据读写（ilst + Keys + XMP）。

支持:
  * ilst 标准标签（©nam ©ART ©alb ©day ©cmt ©too covr desc auth ©xyz 等）
  * GPS：©xyz (ISO-6709) 与 QuickTime loci box
  * Android/厂商 Keys box (uuid A2394F525A9B4F4A)，保证不被破坏
  * XMP packet (uuid BE7ACFCB-97A9-42E8-9C71-999491E3AFAC)
  * 写入时自动修补 stco / co64，保证 mdat 偏移正确
"""

from __future__ import annotations

import re
import struct
import uuid as uuidlib
from dataclasses import dataclass, field

from . import atoms

# ---- ilst 原子标签 → 可读名称 / 界面分组 -------------------------------
ILST_TAGS: dict[str, dict] = {
    "©nam": {"label": "标题", "group": "基础信息", "type": "text"},
    "©ART": {"label": "艺术家/作者", "group": "基础信息", "type": "text"},
    "©alb": {"label": "专辑", "group": "基础信息", "type": "text"},
    "©cmt": {"label": "描述/备注", "group": "基础信息", "type": "text"},
    "©day": {"label": "创建日期", "group": "基础信息", "type": "text"},
    "©gen": {"label": "类型/分类", "group": "基础信息", "type": "text"},
    "desc": {"label": "说明", "group": "基础信息", "type": "text"},
    "auth": {"label": "作者(Auth)", "group": "基础信息", "type": "text"},
    "cprt": {"label": "版权", "group": "基础信息", "type": "text"},
    "©too": {"label": "编码软件", "group": "基础信息", "type": "text"},
    "©enc": {"label": "编码人", "group": "基础信息", "type": "text"},
    "©wrt": {"label": "作曲", "group": "基础信息", "type": "text"},
    "ldes": {"label": "长描述", "group": "基础信息", "type": "text"},
    "©xyz": {"label": "GPS 经纬度", "group": "GPS", "type": "text"},
    "loci": {"label": "位置(loci)", "group": "GPS", "type": "text"},
    "covr": {"label": "封面图", "group": "封面", "type": "binary"},
    "trkn": {"label": "音轨号", "group": "其他", "type": "int"},
    "disk": {"label": "碟片号", "group": "其他", "type": "int"},
    "tmpo": {"label": "BPM", "group": "其他", "type": "int"},
    "cpil": {"label": "内容分级", "group": "其他", "type": "int"},
    "pgap": {"label": "是否纯音频", "group": "其他", "type": "int"},
    "stik": {"label": "Media Type", "group": "其他", "type": "int"},
    "rate": {"label": "评分", "group": "其他", "type": "text"},
}

# 可写的文本型标签（写其余的直接忽略）
WRITABLE_TEXT = {k for k, v in ILST_TAGS.items() if v["type"] == "text"}

# mdta Keys 体系（iPhone/安卓视频）常见 key → 界面标签。
# 这类文件的值放在 ilst 里但 item 类型是 4 字节序号，需要 Keys box 反查名字。
KEY_LABELS: dict[str, dict] = {
    "com.apple.quicktime.make": {"label": "设备制造商", "group": "设备信息", "type": "text"},
    "com.apple.quicktime.model": {"label": "设备型号", "group": "设备信息", "type": "text"},
    "com.apple.quicktime.software": {"label": "软件/系统", "group": "设备信息", "type": "text"},
    "com.apple.quicktime.creationdate": {"label": "创建日期", "group": "基础信息", "type": "text"},
    "com.apple.quicktime.location.ISO6709": {"label": "GPS 定位(ISO6709)", "group": "GPS", "type": "text"},
    "com.apple.quicktime.title": {"label": "标题", "group": "基础信息", "type": "text"},
    "com.apple.quicktime.artist": {"label": "艺术家/作者", "group": "基础信息", "type": "text"},
    "com.apple.quicktime.comment": {"label": "描述/备注", "group": "基础信息", "type": "text"},
    "com.apple.quicktime.description": {"label": "说明", "group": "基础信息", "type": "text"},
    "com.apple.quicktime.copyright": {"label": "版权", "group": "基础信息", "type": "text"},
    "com.apple.quicktime.author": {"label": "作者(Auth)", "group": "基础信息", "type": "text"},
    "com.apple.quicktime.keywords": {"label": "关键词", "group": "基础信息", "type": "text"},
    "com.android.version": {"label": "安卓版本", "group": "设备信息", "type": "text"},
    "com.android.model": {"label": "设备型号", "group": "设备信息", "type": "text"},
    "com.android.manufacturer": {"label": "设备制造商", "group": "设备信息", "type": "text"},
}

# data box 类型标识
DT_IMPLICIT = 0
DT_UTF8 = 1
DT_UTF16 = 2
DT_JPEG = 13
DT_PNG = 14
DT_SIGNED = 21
DT_UNSIGNED = 22
DT_FLOAT32 = 23
DT_FLOAT64 = 64
DT_BE_SIGNED = 65
DT_BE_UNSIGNED = 66

KEYS_UUID = uuidlib.UUID("a2394f52-5a9b-4f4a-a4c2-5b0e2c7b1e1f")
XMP_UUID = uuidlib.UUID("be7acfcb-97a9-42e8-9c71-999491e3afac")


def _u(s: str) -> bytes:
    """
    ilst 标签名 → 单字节 atom type。
    '\xa9' (©) 在 UTF-8 中非法，必须用 latin-1 编码，否则会变成替换字符。
    注意：此函数只用于 atom 名，不要拿去编码文本值。
    """
    return s.encode("latin-1", "replace")


def _ut(text: str) -> bytes:
    """文本值 → UTF-8 字节（data box type 1 即 UTF-8）。"""
    return text.encode("utf-8")


def _decode_text(payload: bytes, dtype: int) -> str:
    if dtype == DT_UTF16:
        return payload.decode("utf-16-be", "ignore").rstrip("\x00")
    if dtype in (DT_UTF8, DT_IMPLICIT, 0):
        return payload.decode("utf-8", "ignore").rstrip("\x00")
    return payload.decode("utf-8", "ignore")


# ---------------------------------------------------------------- 读取
def _decode_data(payload: bytes) -> tuple[int, bytes]:
    """data box 的 payload -> (type_indicator, value_bytes)"""
    if len(payload) < 8:
        return DT_IMPLICIT, payload
    dt = struct.unpack_from(">I", payload, 0)[0] & 0x00FFFFFF
    return dt, payload[8:]


def _encode_data(text: str) -> bytes:
    body = _ut(text)
    return struct.pack(">II", DT_UTF8, 0) + body


def _build_item(tag: bytes, data_payload: bytes) -> atoms.Box:
    data = atoms.Box(type=b"data", payload=bytearray(data_payload))
    # is_container 必须为 True：ilst 子项虽然是数据 box，但内含 data 子 box，
    # 重建时若不按容器序列化，回读会丢失全部内容。
    return atoms.Box(type=tag, children=[data], is_container=True)


def read_ilst(boxes: list[atoms.Box]) -> dict:
    """
    读出 ilst 与 Keys 中的可读元数据。

    ilst 的位置在不同来源的文件里不固定（moov/udta/meta/ilst 或
    moov/meta/ilst），所以收集全树所有 ilst 合并。Keys(mdta) 体系里
    ilst 的 item 类型是 4 字节序号，需通过 Keys box 反查真实名字。
    """
    out: dict = {"ilst": {}, "keys": {}, "key_names": [], "xmp": "", "loci": None}

    # ---- Keys box：4CC 是 'uuid' + user_type A2394F52...（iPhone/安卓视频），
    #      FullBox(version/flags) + entry_count + [key_size(4)+'mdta'+name]... ----
    for ub in atoms.collect(boxes, b"uuid"):
        if ub.user_type and _is_keys_uuid(ub.user_type):
            names = _parse_keys(ub)
            if names:
                out["key_names"] = names

    def key_of_item(raw: bytes) -> str | None:
        """4 字节序号 item 类型 → Keys 表里的名字；不是序号返回 None。"""
        if len(raw) == 4 and out["key_names"]:
            idx = int.from_bytes(raw, "big")
            if 1 <= idx <= len(out["key_names"]):
                return out["key_names"][idx - 1]
        return None

    def take_items(ilst_box: atoms.Box) -> None:
        for item in ilst_box.children:
            # 注意：atom 名是单字节 latin-1，'\xa9'(©) 在 UTF-8 下非法，
            # 必须用 latin-1 解码，否则会变成替换字符导致标签无法匹配。
            raw_tag = item.type
            if all(32 <= b < 127 or b == 0xA9 for b in raw_tag):
                tag = raw_tag.decode("latin-1", "replace").rstrip("\x00")
            else:
                named = key_of_item(raw_tag)
                if not named:
                    continue   # 无法映射的序号项，跳过避免乱码行
                tag = named
            if tag in out["ilst"]:
                continue     # 多个 ilst 合并时先到先得
            data_boxes = [d for d in item.children if d.type == b"data"]
            if raw_tag == b"loci":
                # loci 记录：新版在 data 子 box 里；旧版裸 payload 即记录本身
                if data_boxes:
                    val = _decode_data(bytes(data_boxes[0].payload))[1]
                elif not item.is_container:
                    val = bytes(item.payload)
                else:
                    continue
                out["loci"] = _parse_loci(val) if len(val) >= 14 else None
                if out["loci"]:
                    g = out["loci"]
                    out["ilst"]["loci"] = {"type": "text",
                                           "value": f"{g['lat']:.6f}, {g['lon']:.6f}"}
                continue
            if data_boxes:
                dt, val = _decode_data(bytes(data_boxes[0].payload))
            elif not item.is_container and item.payload:
                # 裸 payload（非标准写入的残留），按 data box 布局尽力解
                dt, val = _decode_data(bytes(item.payload))
            else:
                continue
            if tag == "covr":
                out["ilst"][tag] = {"type": "binary",
                                    "mime": "jpeg" if dt == DT_JPEG else "png",
                                    "size": len(val)}
            else:
                out["ilst"][tag] = {"type": "text", "value": _decode_text(val, dt)}

    for ilst in atoms.collect(boxes, b"ilst"):
        take_items(ilst)

    # XMP：uuid box 未必挂在 udta 下，全树收集
    for ub in atoms.collect(boxes, b"uuid"):
        if ub.user_type:
            try:
                if uuidlib.UUID(bytes=ub.user_type) == XMP_UUID:
                    out["xmp"] = bytes(ub.payload).decode("utf-8", "ignore")
            except Exception:
                pass
    return out


def _parse_keys(box: atoms.Box) -> list[str]:
    """
    解析 mdta Keys box（iPhone/安卓视频的 uuid A2394F52...），
    返回 1-based 顺序的 key 名列表。

    真实布局（ISO BMFF 'Keys'）：
      version/flags(4) + entry_count(4)
      + entry_count × [ key_size(4) + key_namespace(4) + key_name(key_size-8) ]
    Keys box 只含名字表；对应的值在同容器 ilst 里以 4 字节序号为类型的 item 承载。
    """
    data = bytes(box.payload)
    names: list[str] = []
    try:
        if len(data) < 8:
            return names
        count = struct.unpack_from(">I", data, 4)[0]
        pos = 8
        for _ in range(count):
            if pos + 8 > len(data):
                break
            ksize = struct.unpack_from(">I", data, pos)[0]
            if ksize < 8 or pos + ksize > len(data):
                break
            name = data[pos + 8:pos + ksize].decode("utf-8", "replace")
            names.append(name)
            pos += ksize
    except Exception:
        pass
    return names


def _parse_loci(data: bytes) -> dict | None:
    """
    QuickTime loci 记录（16.16 定点）：
      lang(2) + country(2) + lat(4)@4 + lon(4)@8 + alt(4)@12
    """
    if len(data) < 16:
        return None
    try:
        lat = struct.unpack_from(">i", data, 4)[0] / 65536.0
        lon = struct.unpack_from(">i", data, 8)[0] / 65536.0
        out = {"lat": lat, "lon": lon}
        alt = struct.unpack_from(">i", data, 12)[0] / 65536.0
        if alt:
            out["alt"] = round(alt, 3)
        return out
    except Exception:
        return None


# ---------------------------------------------------------------- 写入
def _relocate_chunk_offsets(boxes: list[atoms.Box],
                            old_starts: list[int], old_total: int) -> None:
    """
    moov 重建后，按「旧布局 → 新布局」逐段平移 chunk offset（stco/co64）。

    chunk offset 是绝对文件偏移，指向 mdat 内部。moov 体积变化后，
    只有位于 moov 之后的顶层 box 位置会平移：
      * faststart（moov 在前）：mdat 整体后移，offsets += delta
      * moov 在文件尾：mdat 位置不变，offsets 必须保持原值
    旧实现一刀切加 delta，对 moov-at-end 的文件是直接改坏视频。
    这里改为：chunk offset 落在哪个旧顶层段，就加那一段的位置差。
    """
    shifts: list[int] = []
    new_off = 0
    for i, b in enumerate(boxes):
        size = len(b.build())
        shifts.append(new_off - old_starts[i])
        new_off += size
    bounds = list(old_starts) + [old_total]

    def shift_of(v: int) -> int | None:
        for i in range(len(bounds) - 1):
            if bounds[i] <= v < bounds[i + 1]:
                return shifts[i]
        return None

    for stco in atoms.collect(boxes, b"stco"):
        payload = bytearray(stco.payload)
        if len(payload) < 8:
            continue
        count = struct.unpack_from(">I", payload, 4)[0]
        off = 8
        for _ in range(count):
            if off + 4 > len(payload):
                break
            v = struct.unpack_from(">I", payload, off)[0]
            s = shift_of(v)
            if s:
                struct.pack_into(">I", payload, off, (v + s) & 0xFFFFFFFF)
            off += 4
        stco.payload = payload
    for co64 in atoms.collect(boxes, b"co64"):
        payload = bytearray(co64.payload)
        if len(payload) < 8:
            continue
        count = struct.unpack_from(">I", payload, 4)[0]
        off = 8
        for _ in range(count):
            if off + 8 > len(payload):
                break
            v = struct.unpack_from(">Q", payload, off)[0]
            s = shift_of(v)
            if s:
                struct.pack_into(">Q", payload, off, v + s)
            off += 8
        co64.payload = payload


def _normalize_cover(cover: bytes) -> bytes:
    """把任意图片转成 JPEG。已是 JPEG 则原样返回。

    covr 里放 WebP/HEIC/BMP 等格式时，很多播放器直接不显示，
    所以统一转成兼容性最好的 JPEG。
    """
    if cover[:2] == b"\xff\xd8":
        return cover
    try:
        import io as _io
        from PIL import Image
        im = Image.open(_io.BytesIO(cover))
        im.load()
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        out = _io.BytesIO()
        im.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return cover


def _apply_ilst_changes(boxes: list[atoms.Box], *, changes: dict,
                        cover: bytes | None = None, gps: dict | None = None,
                        drop: list[str] | None = None, xmp: str | None = None,
                        drop_keys: list[str] | None = None,
                        drop_keys_all: bool = False,
                        creation_time: str | None = None) -> None:
    """
    在已解析的 box 树上应用全部元数据变更（不负责解析/序列化/偏移修补）。

    boxes 须包含 moov（整体文件的顶层列表，或仅 [moov] 的流式模式）。
    """
    udta = atoms.ensure(boxes, [b"moov", b"udta"])
    meta = None
    for ch in udta.children:
        if ch.type == b"meta":
            meta = ch
            break
    if meta is None:
        meta = atoms.Box(type=b"meta", is_container=True, is_full=True)
        # iTunes 规范要求 hdlr 排在 ilst 前
        hdlr = atoms.Box(type=b"hdlr", is_container=True, is_full=True,
                         version_flags=b"\x00\x00\x00\x00",
                         payload=bytearray(b"\x00" * 8 + b"mdirappl" + b"\x00" * 9))
        meta.children.append(hdlr)
        udta.children.insert(0, meta)
    ilst = atoms.ensure(meta.children, [b"ilst"])

    def _find_idx(tag_bytes: bytes) -> int | None:
        # 用身份比较而非 == ，Box 是 dataclass，值相等的不同节点会被误判为同一个
        for i, c in enumerate(ilst.children):
            if c.type == tag_bytes:
                return i
        return None

    def set_text(tag: str, value: str) -> None:
        tb = _u(tag)
        idx = _find_idx(tb)
        node = _build_item(tb, _encode_data(value))
        if idx is None:
            ilst.children.append(node)
        else:
            ilst.children[idx] = node

    def set_binary(tag: str, blob: bytes, dt: int) -> None:
        tb = _u(tag)
        payload = struct.pack(">II", dt, 0) + blob
        node = atoms.Box(type=tb, children=[atoms.Box(type=b"data", payload=bytearray(payload))],
                         is_container=True)
        idx = _find_idx(tb)
        if idx is None:
            ilst.children.append(node)
        else:
            ilst.children[idx] = node

    for tag in (drop or []):
        for i in range(len(ilst.children) - 1, -1, -1):
            if ilst.children[i].type == _u(tag):
                ilst.children.pop(i)

    for tag, val in (changes or {}).items():
        if val is None or val == "":
            continue
        # 白名单外/二进制型标签拒绝当文本写：
        # covr 写入占位文本会毁掉封面，序号项/未知 4CC 写入会污染容器
        if tag not in WRITABLE_TEXT:
            continue
        set_text(tag, str(val))

    # mdta Keys 体系（iPhone/安卓）的设备痕迹清理
    if drop_keys_all or drop_keys:
        key_names: list[str] = []
        for ub in atoms.collect(boxes, b"uuid"):
            if ub.user_type and _is_keys_uuid(ub.user_type):
                key_names = _parse_keys(ub) or key_names
        import fnmatch as _fn
        for i in range(len(ilst.children) - 1, -1, -1):
            t = ilst.children[i].type
            # 序号 item：类型是 4 字节非可见字符
            if len(t) == 4 and not all(32 <= b < 127 or b == 0xA9 for b in t):
                if drop_keys_all:
                    ilst.children.pop(i)
                    continue
                idx = int.from_bytes(t, "big")
                if 1 <= idx <= len(key_names) and any(
                        _fn.fnmatch(key_names[idx - 1], pat) for pat in drop_keys):
                    ilst.children.pop(i)
    if drop_keys_all:
        for i in range(len(udta.children) - 1, -1, -1):
            if udta.children[i].type == b"uuid" and udta.children[i].user_type \
                    and _is_keys_uuid(udta.children[i].user_type):
                udta.children.pop(i)

    if cover:
        # 非 JPEG/PNG 一律先转成 JPEG：播放器对 covr 里的 WebP/HEIC/BMP
        # 支持度很差，表现为"封面写进去了但看不到"。
        cover = _normalize_cover(cover)
        dt = DT_JPEG if cover[:2] == b"\xff\xd8" else (DT_PNG if cover[:8] == b"\x89PNG\r\n\x1a\n" else DT_IMPLICIT)
        set_binary("covr", cover, dt)

    if gps and gps.get("lat") is not None and gps.get("lon") is not None:
        lat, lon = float(gps["lat"]), float(gps["lon"])
        alt = gps.get("alt")
        xyz = f"{lat:+.6f}{lon:+.6f}" + (f"{float(alt):+.3f}" if alt is not None else "") + "/"
        set_text("©xyz", xyz)
        lat_i = int(round(lat * 65536))   # loci 定点数 16.16
        lon_i = int(round(lon * 65536))
        alt_i = int(round(float(alt) * 65536)) if alt is not None else 0
        body = (b"\x55\xc4" + b"\x00\x00" + struct.pack(">ii", lat_i, lon_i) +
                struct.pack(">i", max(-2 ** 31, min(2 ** 31 - 1, alt_i))))
        # loci 是 ilst 子项，必须内含 data 子 box；裸 payload 会在下次解析时损坏
        node = atoms.Box(type=b"loci", is_container=True,
                         children=[atoms.Box(type=b"data",
                                             payload=bytearray(struct.pack(">II", DT_IMPLICIT, 0) + body))])
        idx = _find_idx(b"loci")
        if idx is None:
            ilst.children.append(node)
        else:
            ilst.children[idx] = node

    # 创建时间：mvhd/mdhd/tkhd（播放器与资源管理器）+ ©day + Keys creationdate
    if creation_time:
        dt_obj = parse_time_text(creation_time)
        if dt_obj is not None:
            secs = _local_to_mp4_seconds(dt_obj)
            for header in (b"mvhd", b"mdhd", b"tkhd"):
                for hb in atoms.collect(boxes, header):
                    _set_creation_seconds(hb, secs)
            set_text("©day", dt_obj.strftime("%Y-%m-%d %H:%M:%S"))
            _set_keys_value(boxes, ilst, "com.apple.quicktime.creationdate",
                            dt_obj.strftime("%Y-%m-%dT%H:%M:%S"))

    # XMP
    if xmp is not None:
        for ch in list(udta.children):
            if ch.type == b"uuid" and ch.user_type and _is_xmp_uuid(ch.user_type):
                udta.children.remove(ch)
        if xmp.strip():
            node = atoms.Box(type=b"uuid", payload=bytearray(xmp.encode("utf-8")),
                             user_type=XMP_UUID.bytes)
            udta.children.append(node)

    # 移除空 ilst / 空 meta，避免播放器困惑
    if not ilst.children:
        meta.children.remove(ilst)
    if not any(c.type != b"hdlr" for c in meta.children):
        udta.children.remove(meta)
    if not udta.children:
        atoms.remove(boxes, [b"moov", b"udta"])


def write_ilst(src: bytes, changes: dict, *, cover: bytes | None = None,
               gps: dict | None = None, drop: list[str] | None = None,
               xmp: str | None = None,
               drop_keys: list[str] | None = None,
               drop_keys_all: bool = False,
               creation_time: str | None = None) -> bytes:
    """
    在保留其余一切的前提下修改元数据（内存版，适合中小文件与测试）。

    changes      : {ilst标签: 文本值}   例 {"©nam": "标题"}（仅白名单内文本标签生效）
    cover        : JPEG/PNG 字节，写入 covr
    gps          : {"lat":.., "lon":..[, "alt":..]} 同时写 ©xyz 与 loci
    drop         : 要删除的 ilst 标签列表（隐私清除）
    xmp          : 完整 XMP 文本，写入 uuid box
    drop_keys    : mdta Keys 体系里要删除的 key 名（支持 fnmatch 通配）
    drop_keys_all: 删除整个 Keys box 与全部序号 ilst 项（设备痕迹清理）
    creation_time: "YYYY-MM-DD HH:MM:SS"，写 mvhd/mdhd/tkhd + ©day + Keys
    """
    boxes = atoms.parse(src)
    old_starts = [b.start for b in boxes]
    old_total = len(src)

    _apply_ilst_changes(boxes, changes=changes, cover=cover, gps=gps, drop=drop,
                        xmp=xmp, drop_keys=drop_keys, drop_keys_all=drop_keys_all,
                        creation_time=creation_time)

    _relocate_chunk_offsets(boxes, old_starts, old_total)
    return atoms.serialize(boxes)


def _copy_stream(src, dst, nbytes: int, chunk: int = 4 * 1024 * 1024) -> None:
    remaining = nbytes
    while remaining > 0:
        block = src.read(min(chunk, remaining))
        if not block:
            break
        dst.write(block)
        remaining -= len(block)


def _shift_chunk_offsets_from(boxes: list[atoms.Box], file_pos: int, delta: int) -> None:
    """
    流式写入用：moov 重建后，文件里位于 moov 之后的区域整体平移 delta，
    stco/co64 中指向该区域的绝对 chunk offset 同步 +delta。
    指向 moov 之前（mdat 在前）的偏移保持不变。
    """
    if delta == 0:
        return
    for stco in atoms.collect(boxes, b"stco"):
        payload = bytearray(stco.payload)
        if len(payload) < 8:
            continue
        count = struct.unpack_from(">I", payload, 4)[0]
        off = 8
        for _ in range(count):
            if off + 4 > len(payload):
                break
            v = struct.unpack_from(">I", payload, off)[0]
            if v >= file_pos:
                struct.pack_into(">I", payload, off, (v + delta) & 0xFFFFFFFF)
            off += 4
        stco.payload = payload
    for co64 in atoms.collect(boxes, b"co64"):
        payload = bytearray(co64.payload)
        if len(payload) < 8:
            continue
        count = struct.unpack_from(">I", payload, 4)[0]
        off = 8
        for _ in range(count):
            if off + 8 > len(payload):
                break
            v = struct.unpack_from(">Q", payload, off)[0]
            if v >= file_pos:
                struct.pack_into(">Q", payload, off, v + delta)
            off += 8
        co64.payload = payload


def write_ilst_file(src_path: str, dst_path: str, changes: dict, *, cover=None,
                    gps=None, drop=None, xmp=None, drop_keys=None,
                    drop_keys_all: bool = False,
                    creation_time: str | None = None) -> tuple[int, bool]:
    """
    流式写元数据：只把 moov 读进内存，mdat 等大块数据原样流拷贝。
    内存占用与文件大小无关（几 GB 的视频也只占几 MB），解决大文件
    整体读入导致卡死/内存暴涨的问题。

    moov 体积不变时直接原地覆盖（不产生临时文件）；体积变化时把
    结果写入 dst_path，由调用方 os.replace。返回 (新文件大小, 是否原地)。
    """
    tops, total = atoms.scan_top_level(src_path)
    span = next(((s, z) for t, s, z in tops
                 if t == b"moov" and z <= 512 * 1024 * 1024), None)

    if span is None:
        # 没有 moov（损坏文件）：新 moov 追加到文件尾，既有偏移不动
        moov = atoms.Box(type=b"moov", is_container=True)
        _apply_ilst_changes([moov], changes=changes, cover=cover, gps=gps,
                            drop=drop, xmp=xmp, drop_keys=drop_keys,
                            drop_keys_all=drop_keys_all,
                            creation_time=creation_time)
        new_moov = atoms.serialize([moov])
        with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
            _copy_stream(src, dst, total)
            dst.write(new_moov)
        return total + len(new_moov), False

    start, size = span
    with open(src_path, "rb") as f:
        f.seek(start)
        moov_blob = f.read(size)
    boxes = atoms.parse(moov_blob)
    _apply_ilst_changes(boxes, changes=changes, cover=cover, gps=gps,
                        drop=drop, xmp=xmp, drop_keys=drop_keys,
                        drop_keys_all=drop_keys_all, creation_time=creation_time)
    new_moov = atoms.serialize(boxes)
    if len(new_moov) < 16:
        raise ValueError("写入结果异常：moov 过小")
    delta = len(new_moov) - size
    if delta:
        _shift_chunk_offsets_from(boxes, start + size, delta)
        new_moov = atoms.serialize(boxes)

    if delta == 0:
        with open(src_path, "r+b") as f:
            f.seek(start)
            f.write(new_moov)
        return total, True

    with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
        _copy_stream(src, dst, start)
        dst.write(new_moov)
        src.seek(start + size)
        _copy_stream(src, dst, total - start - size)
    return total + delta, False


# ---- 时间解析（视频创建时间）------------------------------------------
# MP4 的 mvhd/mdhd/tkhd creation_time 是自 1904-01-01 起的 UTC 秒数
_MP4_EPOCH_OFFSET = 2082844800
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                 "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
                 "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d", "%Y-%m-%d")


def parse_time_text(text: str):
    """接受 2024-03-15 14:30:00 / 2024-03-15T14:30 / 2024-03-15 等，返回本地时间。"""
    import datetime as _dt
    t = (text or "").strip()
    if not t:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _local_to_mp4_seconds(dt_obj) -> int:
    unix = int(dt_obj.timestamp())      # 按本地时区解释
    return max(0, unix + _MP4_EPOCH_OFFSET)


def mp4_seconds_to_text(secs: int) -> str:
    """MP4 时间 → 本地时间文本；无效（0/超界）返回空串。"""
    import datetime as _dt
    unix = secs - _MP4_EPOCH_OFFSET
    if unix <= 0:
        return ""
    try:
        return _dt.datetime.fromtimestamp(unix).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _set_creation_seconds(box: atoms.Box, secs: int) -> None:
    """mvhd/mdhd/tkhd 的 creation_time 位于 version/flags 之后（v0 4字节 / v1 8字节）。"""
    payload = bytearray(box.payload)
    if len(payload) < 8:
        return
    if payload[0] == 1:
        if len(payload) >= 12:
            struct.pack_into(">Q", payload, 4, secs)
    else:
        struct.pack_into(">I", payload, 4, min(secs, 0xFFFFFFFF))
    box.payload = payload


def _set_keys_value(boxes: list[atoms.Box], default_ilst: atoms.Box,
                    key: str, value: str) -> None:
    """
    在已存在的 mdta Keys 体系里写入/更新一个 key 的值（如
    com.apple.quicktime.creationdate）。没有 Keys box 时静默跳过。
    值写入包含序号项的那个 ilst（与 Keys 同体系），没有则用 default_ilst。
    """
    keys_box = None
    for ub in atoms.collect(boxes, b"uuid"):
        if ub.user_type and _is_keys_uuid(ub.user_type):
            keys_box = ub
            break
    if keys_box is None:
        return
    names = _parse_keys(keys_box)
    payload = bytearray(keys_box.payload)
    if key in names:
        idx = names.index(key) + 1
    else:
        idx = len(names) + 1
        if len(payload) >= 8:
            struct.pack_into(">I", payload, 4, len(names) + 1)
        name_b = key.encode("utf-8")
        payload += struct.pack(">I", len(name_b) + 8) + b"mdta" + name_b
        keys_box.payload = payload

    target = default_ilst
    for ib in atoms.collect(boxes, b"ilst"):
        if any(len(c.type) == 4 and not all(32 <= b < 127 or b == 0xA9 for b in c.type)
               for c in ib.children):
            target = ib
            break
    tb = struct.pack(">I", idx)
    node = atoms.Box(type=tb, is_container=True,
                     children=[atoms.Box(type=b"data",
                                         payload=bytearray(struct.pack(">II", DT_UTF8, 0) +
                                                           value.encode("utf-8")))])
    for i, c in enumerate(target.children):
        if c.type == tb:
            target.children[i] = node
            return
    target.children.append(node)


def _is_xmp_uuid(raw: bytes) -> bool:
    try:
        return uuidlib.UUID(bytes=raw) == XMP_UUID
    except Exception:
        return False


def _is_keys_uuid(raw: bytes) -> bool:
    try:
        return uuidlib.UUID(bytes=raw) == KEYS_UUID
    except Exception:
        return False


def set_cover(src: bytes, cover_bytes: bytes) -> bytes:
    return write_ilst(src, {}, cover=cover_bytes)


def remove_cover(src: bytes) -> bytes:
    """删除 covr（封面图），其余元数据保持不变。"""
    return write_ilst(src, {}, drop=["covr"])


def clear_gps(src: bytes) -> bytes:
    return write_ilst(src, {}, drop=["©xyz", "loci"])


# ---- GPS 文本解析 (ISO-6709 / "lat, lon") ------------------------------
# ISO-6709: ±lat±lon[/] 或 ±lat±lon±alt[/]（iPhone 的
# com.apple.quicktime.location.ISO6709 即最后一种）
_ISO6709 = re.compile(
    r"^\s*([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?\s*/?\s*$")


def parse_gps_text(text: str) -> dict | None:
    t = text.strip()
    if not t:
        return None
    m = _ISO6709.match(t)
    if m:
        out = {"lat": float(m.group(1)), "lon": float(m.group(2))}
        if m.group(3) is not None:
            out["alt"] = float(m.group(3))
        return out
    if "," in t:
        parts = t.split(",")
        try:
            return {"lat": float(parts[0]), "lon": float(parts[1])}
        except ValueError:
            return None
    return None
