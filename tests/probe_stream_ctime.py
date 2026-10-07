# -*- coding: utf-8 -*-
"""最小探针：流式写入内存峰值 / 原地写 / 创建时间。仅复用已验证的构造代码。"""
import datetime
import io
import os
import struct
import sys
import tracemalloc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import atoms, quicktime, unified  # noqa: E402

OUT = os.path.join(ROOT, "tests", "_repro")
os.makedirs(OUT, exist_ok=True)
OK = []


def hr(title, cond):
    OK.append(bool(cond))
    print(("  PASS  " if cond else "  FAIL  ") + title)


def box(t, payload):
    return struct.pack(">I", len(payload) + 8) + t + payload


def mvhd_of():
    return box(b"mvhd", struct.pack(">I", 0) + struct.pack(">IIII", 0, 0, 1000, 5000) +
               struct.pack(">i", 0x00010000) + struct.pack(">h", 0x0100) + b"\x00" * 10 +
               struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) +
               b"\x00" * 24 + struct.pack(">I", 2))


def trak_of():
    tkhd = box(b"tkhd", struct.pack(">I", 7) + struct.pack(">IIIII", 0, 0, 1, 0, 5000) +
               b"\x00" * 8 + struct.pack(">hhhh", 0, 0, 0, 0) +
               struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000) +
               struct.pack(">II", 1920 << 16, 1080 << 16))
    hdlr = box(b"hdlr", struct.pack(">I", 0) + b"\x00" * 4 + b"vide" + b"\x00" * 12 + b"VideoHandler\x00")
    stbl = box(b"stbl",
               box(b"stsd", struct.pack(">I", 0) + struct.pack(">I", 1) + box(b"avc1", b"\x00" * 78)) +
               box(b"stts", struct.pack(">I", 0) + struct.pack(">I", 0)) +
               box(b"stsc", struct.pack(">I", 0) + struct.pack(">I", 0)) +
               box(b"stsz", struct.pack(">I", 0) + struct.pack(">II", 0, 0)) +
               box(b"stco", struct.pack(">I", 0) + struct.pack(">I", 1) + struct.pack(">I", 0)))
    minf = box(b"minf", box(b"vmhd", struct.pack(">I", 1) + struct.pack(">HHHH", 0, 0, 0, 0)) +
               box(b"dinf", box(b"dref", struct.pack(">I", 0) + struct.pack(">I", 1) +
                                box(b"url ", struct.pack(">I", 1)))) + stbl)
    mdia = box(b"mdia", box(b"mdhd", struct.pack(">I", 0) + struct.pack(">IIII", 0, 0, 1000, 5000) +
                            struct.pack(">HH", 0x55C4, 0)) + hdlr + minf)
    return box(b"trak", tkhd + mdia)


def udta_of(extra=b"", keys_box=b""):
    ilst = box(b"ilst",
               box(b"\xa9nam", box(b"data", struct.pack(">II", 1, 0) + "原始标题".encode("utf-8"))) +
               extra)
    meta = box(b"meta", struct.pack(">I", 0) +
               box(b"hdlr", struct.pack(">I", 0) + b"\x00" * 4 + b"mdirappl" + b"\x00" * 9) + ilst)
    return box(b"udta", meta + keys_box)


def make_video(path, mb=0, moov_first=False, extra=b"", keys_box=b""):
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    mdat = box(b"mdat", b"MDATMAGIC" + os.urandom(max(mb * 1024 * 1024, 4088) - 9))
    moov = box(b"moov", mvhd_of() + trak_of() + udta_of(extra, keys_box))
    data = (ftyp + moov + mdat) if moov_first else (ftyp + mdat + moov)
    with open(path, "wb") as f:
        f.write(data)
    boxes = atoms.parse(data)
    stco = atoms.walk(boxes, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stco"])
    pos, mdat_start = 0, None
    while pos + 8 <= len(data):
        size = struct.unpack_from(">I", data, pos)[0]
        if data[pos + 4:pos + 8] == b"mdat":
            mdat_start = pos + 8
            break
        pos += size if size >= 8 else 8
    payload = bytearray(stco.payload)
    struct.pack_into(">I", payload, 8, mdat_start or 0)
    stco.payload = payload
    with open(path, "wb") as f:
        f.write(atoms.serialize(boxes))
    return path


def stco_ok(path):
    data = open(path, "rb").read()
    stco = atoms.walk(atoms.parse(data), [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stco"])
    off = struct.unpack_from(">I", bytes(stco.payload), 8)[0]
    return data[off:off + 9] == b"MDATMAGIC"


print("== A. 流式写入：120MB 文件内存峰值 ==")
pA = make_video(os.path.join(OUT, "pA_stream.mp4"), mb=120)
tracemalloc.start()
w = unified.write_video(pA, changes={"©nam": "流式写入的新标题，长度与前不同"})
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
mA = unified.read_video(pA)
hr(f"写入成功 {w}", not w)
hr("回读正确", mA["ilst"].get("©nam", {}).get("value") == "流式写入的新标题，长度与前不同")
hr(f"峰值内存 {peak / 1048576:.1f}MB < 150MB", peak < 150 * 1048576)
hr("stco 有效", stco_ok(pA))

print("== B. 同字节数标题 → 原地写（moov 尺寸不变） ==")
pB = make_video(os.path.join(OUT, "pB_inplace.mp4"), mb=4)
size0 = os.path.getsize(pB)
# "原始标题" 与 "同样标题" 都是 4 个汉字 = 12 字节
total, in_place = quicktime.write_ilst_file(pB, pB + ".tmp", {"©nam": "同样标题"})
hr(f"返回 in_place=True（实际 {in_place}）", in_place is True)
hr("文件大小不变", os.path.getsize(pB) == size0)
hr("不产生临时文件", not os.path.exists(pB + ".tmp"))
mB = unified.read_video(pB)
hr("原地写回读正确", mB["ilst"].get("©nam", {}).get("value") == "同样标题")
hr("stco 有效", stco_ok(pB))

print("== C. 变尺寸写入 → 走 tmp + replace，源文件正确 ==")
pC = make_video(os.path.join(OUT, "pC_delta.mp4"), mb=4)
size0 = os.path.getsize(pC)
w = unified.write_video(pC, changes={"©nam": "这次标题变长了"})
hr(f"写入成功 {w}", not w)
hr("无临时文件残留", not os.path.exists(pC + ".mforge.tmp"))
mC = unified.read_video(pC)
hr("回读正确", mC["ilst"].get("©nam", {}).get("value") == "这次标题变长了")
hr("stco 有效", stco_ok(pC))

print("== D. 创建时间端到端 ==")
KEYS_GUID = __import__("uuid").UUID("a2394f52-5a9b-4f4a-a4c2-5b0e2c7b1e1f").bytes
kn = [b"com.apple.quicktime.make", b"com.apple.quicktime.creationdate"]
kp = struct.pack(">I", 0) + struct.pack(">I", len(kn))
for n in kn:
    kp += struct.pack(">I", len(n) + 8) + b"mdta" + n
keys_box = struct.pack(">I", 8 + 16 + len(kp)) + b"uuid" + KEYS_GUID + kp
pD = make_video(os.path.join(OUT, "pD_ctime.mp4"), keys_box=keys_box)
w = unified.write_video(pD, creation_time="2024-03-15 14:30:00")
hr(f"写入无警告 {w}", not w)
mD = unified.read_video(pD)
hr("creation_time 回读", mD.get("creation_time") == "2024-03-15 14:30:00")
hr("©day 已写", mD["ilst"].get("©day", {}).get("value") == "2024-03-15 14:30:00")
hr("Keys creationdate 已写",
   mD["ilst"].get("com.apple.quicktime.creationdate", {}).get("value") == "2024-03-15T14:30:00")
expect = int(datetime.datetime(2024, 3, 15, 14, 30, 0).timestamp())
mvhd = atoms.walk(atoms.parse(open(pD, "rb").read()), [b"moov", b"mvhd"])
got = struct.unpack_from(">I", bytes(mvhd.payload), 4)[0]
hr("mvhd creation_time 秒数正确", got == expect + 2082844800)
hr("stco 有效", stco_ok(pD))

print("== E. 非法时间 → 警告且不破坏其他改动 ==")
pE = make_video(os.path.join(OUT, "pE_badtime.mp4"))
w = unified.write_video(pE, changes={"©cmt": "备注X"}, creation_time="不是时间")
hr("返回格式警告", any("创建时间" in x for x in w))
mE = unified.read_video(pE)
hr("其他改动正常", mE["ilst"].get("©cmt", {}).get("value") == "备注X")

print(f"\n{'全部通过' if all(OK) else '存在失败'}: {sum(OK)}/{len(OK)}")
sys.exit(0 if all(OK) else 1)
