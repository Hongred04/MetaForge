"""
atoms.py —— ISO BMFF (MP4/MOV/M4A/3GP) 原子(box) 的解析与重建。

参考项目借鉴:
  * ExifTool 的 QuickTime.pm  —— ilst 标签映射与 data box 类型处理
  * XuanRanDev/exif-editor    —— 保留厂商私有 tag 的写入策略
  * 标准: ISO/IEC 14496-12 (box 结构)、ISO/IEC 14496-14 (stco/co64)

设计要点:
  1. 解析成树, 便于定位 moov/udta/meta/ilst。
  2. 修改后重建整棵树, 关键风险是 moov 体积变化会让 mdat 偏移失效,
     必须同步修补 stco / co64 (chunk offset), 否则视频无法播放。
  3. 支持 64 位 largesize、uuid box、以及 iTunes 风格的 meta FullBox 变体。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

# 需要递归进入的容器 box
CONTAINERS = {
    b"moov", b"trak", b"mdia", b"minf", b"stbl", b"udta", b"ilst",
    b"edts", b"dinf", b"moof", b"traf", b"mfra", b"skip", b"free",
    b"meta", b"ilst", b"wave", b"sinf", b"schi", b"ipro", b"mvex",
}

# 不进入的纯数据 box（虽在 CONTAINERS 里但有特殊前置头）
FULL_BOX = {b"meta"}  # meta 通常带 4 字节 version/flags


@dataclass(eq=False)   # 按身份比较：值相等的不同 box 不能互相替换
class Box:
    """一个 atom 节点。payload 保存原始(或重建后的)完整 body 字节。"""

    type: bytes
    payload: bytearray = field(default_factory=bytearray)
    children: list["Box"] = field(default_factory=list)
    is_container: bool = False
    is_full: bool = False          # 是否带 version/flags 头
    version_flags: bytes = b"\x00\x00\x00\x00"
    user_type: bytes | None = None  # uuid box 的 16 字节 extended type
    start: int = 0                 # 文件内起始偏移（解析时填）
    header_len: int = 8

    # ---------- 序列化 ----------
    def build(self) -> bytes:
        if self.is_container:
            body = bytearray()
            if self.is_full:
                body += self.version_flags
            for c in self.children:
                body += c.build()
            payload = bytes(body)
        else:
            payload = bytes(self.payload)

        head = bytearray()
        size = len(payload) + 8
        if self.user_type is not None:
            size += 16
        if size <= 0xFFFFFFFF:
            head += struct.pack(">I", size)
        else:
            head += struct.pack(">I", 1)
            head += struct.pack(">Q", size + 8)
        head += self.type
        if self.user_type is not None:
            head += self.user_type
        return bytes(head) + payload

    @property
    def size(self) -> int:
        return len(self.build())


# ---------------------------------------------------------------- 解析
def _read_box(buf: memoryview, pos: int, end: int, parent_type: bytes = b"") -> tuple[Box | None, int]:
    """从 buf[pos:end] 读出一个 box, 返回 (box, next_pos)。"""
    if pos + 8 > end:
        return None, end
    size = struct.unpack_from(">I", buf, pos)[0]
    btype = bytes(buf[pos + 4:pos + 8])
    header = 8

    if size == 1:
        if pos + 16 > end:
            return None, end
        size = struct.unpack_from(">Q", buf, pos + 8)[0]
        header = 16
    elif size == 0:
        # 延伸到文件末尾
        size = end - pos

    user_type = None
    if btype == b"uuid":
        if pos + header + 16 > end:
            return None, end
        user_type = bytes(buf[pos + header:pos + header + 16])
        header += 16

    if size < header or pos + size > end:
        # 损坏 / 截断：尽量吃掉剩余数据，避免整体失败
        size = max(end - pos, header)
        payload_end = end
    else:
        payload_end = pos + size

    box = Box(
        type=btype,
        payload=bytearray(buf[pos + header:payload_end]),
        user_type=user_type,
        start=pos,
        header_len=header,
    )

    # 容器判定。ilst 的直接子项（如 ©nam）虽是数据 box，但内含 data 子 box，
    # 必须按容器解析，否则回读时 data 内容会丢失。
    inside_ilst = parent_type == b"ilst"
    if btype in CONTAINERS or inside_ilst:
        box.is_container = True
        if btype == b"meta" and not inside_ilst:
            # meta 有两种格式并存：iTunes/MP4 的 FullBox（带 4 字节 version/flags）
            # 与 QuickTime 传统的裸容器。探测 +4 处是否为合法子 box 头来判定：
            #   FullBox  → +4 是真实子 box，size 字段必然 8..剩余长度
            #   裸容器   → 前 4 字节本身是子 box size（被误当 flags），+4 处是
            #              子 box 的 type 字段，按整数读必然巨大越界
            c = pos + header + 4
            if c + 8 <= payload_end:
                csize = struct.unpack_from(">I", buf, c)[0]
                box.is_full = 8 <= csize <= payload_end - c
            else:
                box.is_full = False   # 剩余放不下任何子 box，更说明没有 flags
            if box.is_full:
                box.version_flags = bytes(buf[pos + header:pos + header + 4])
        elif inside_ilst:
            # ilst 子项的 payload 应由一个或多个 data 子 box 恰好铺满。
            # 铺不满（如旧版写入的裸 loci）就按裸数据原样保留，
            # 否则会被强行拆成垃圾子 box，重建时破坏内容。
            p = pos + header
            tiled = p < payload_end
            while p + 8 <= payload_end:
                csize = struct.unpack_from(">I", buf, p)[0]
                if csize < 8 or p + csize > payload_end:
                    tiled = False
                    break
                child, np2 = _read_box(buf, p, payload_end, btype)
                if child is None or np2 <= p or np2 != p + csize:
                    tiled = False
                    break
                box.children.append(child)
                p = np2
            if not tiled or p != payload_end:
                box.children = []
                box.is_container = False
            return box, (pos + size if payload_end == pos + size else payload_end)
        inner_start = pos + header + (4 if box.is_full else 0)
        p = inner_start
        while p < payload_end:
            child, np = _read_box(buf, p, payload_end, btype)
            if child is None or np <= p:
                break
            box.children.append(child)
            p = np
    return box, (pos + size if payload_end == pos + size else payload_end)


def parse(data: bytes | memoryview) -> list[Box]:
    """解析顶层 box 列表。"""
    buf = memoryview(data)
    out: list[Box] = []
    p, end = 0, len(buf)
    while p < end:
        box, np = _read_box(buf, p, end)
        if box is None or np <= p:
            break
        out.append(box)
        p = np
    return out


def scan_top_level(path: str) -> tuple[list[tuple[bytes, int, int]], int]:
    """
    流式扫一遍顶层 box 头（只读头部字节，不载入 payload），
    返回 ([(type, start, size)...], 文件总长)。任意大小的文件都是 O(box数) 内存。

    大视频的 moov 可能在文件尾部（非 faststart），把整个文件读进内存
    或「头尾各读一段再拼接」都会让解析失败——拼接边界会把 mdat 撑成
    一个吞掉 moov 的巨 box。先拿到每个 box 的精确位置，再按需读取。
    """
    tops: list[tuple[bytes, int, int]] = []
    with open(path, "rb") as f:
        f.seek(0, 2)
        total = f.tell()
        pos = 0
        while pos + 8 <= total:
            f.seek(pos)
            hdr = f.read(24)
            if len(hdr) < 8:
                break
            size = struct.unpack_from(">I", hdr, 0)[0]
            btype = bytes(hdr[4:8])
            hlen = 8
            if size == 1:
                if len(hdr) < 16:
                    break
                size = struct.unpack_from(">Q", hdr, 8)[0]
                hlen = 16
            elif size == 0:
                size = total - pos   # 延伸到文件末尾
            if size < hlen or pos + size > total:
                break                # 损坏/截断，停在已确认的部分
            tops.append((btype, pos, size))
            pos += size
    return tops, total


# ---------------------------------------------------------------- 查找
def walk(boxes: list[Box], path: list[bytes]) -> Box | None:
    """按路径查找, 如 walk(boxes, [b'moov', b'udta', b'meta', b'ilst'])"""
    cur: list[Box] = boxes
    found: Box | None = None
    for want in path:
        found = None
        for b in cur:
            if b.type == want:
                found = b
                break
        if found is None:
            return None
        cur = found.children
    return found


def ensure(boxes: list[Box], path: list[bytes], full: set[bytes] | None = None) -> Box:
    """确保路径存在, 缺失则创建。返回最后一级 box。"""
    full = full or set()
    cur = boxes
    node: Box | None = None
    for want in path:
        node = next((b for b in cur if b.type == want), None)
        if node is None:
            node = Box(type=want, is_container=True, is_full=(want in full))
            cur.append(node)
        cur = node.children
    return node


def remove(boxes: list[Box], path: list[bytes]) -> bool:
    """按路径删除节点, 返回是否命中。"""
    if not path:
        return False
    parent: list[Box] = boxes
    for want in path[:-1]:
        nxt = next((b for b in parent if b.type == want), None)
        if nxt is None:
            return False
        parent = nxt.children
    target = path[-1]
    for i, b in enumerate(parent):
        if b.type == target:
            parent.pop(i)
            return True
    return False


def collect(boxes: list[Box], want: bytes) -> list[Box]:
    out: list[Box] = []
    stack = list(boxes)
    while stack:
        b = stack.pop()
        if b.type == want:
            out.append(b)
        if b.is_container:
            stack.extend(b.children)
    return out


def serialize(boxes: list[Box]) -> bytes:
    return b"".join(b.build() for b in boxes)
