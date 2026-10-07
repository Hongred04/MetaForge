# -*- coding: utf-8 -*-
"""视频元数据修复验证：moov 位置/大小、mdta Keys、loci、stco 有效性、隐私清理。"""
import io
import os
import struct
import sys
import uuid as uuidlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from core import atoms, privacy, quicktime, unified  # noqa: E402

OUT = os.path.join(ROOT, "tests", "_repro")
os.makedirs(OUT, exist_ok=True)

RESULTS = []


def hr(title, ok):
    RESULTS.append(ok)
    print(("  PASS  " if ok else "  FAIL  ") + title)


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


def ilst_udta(extra_items=b"", keys_box=b""):
    ilst = box(b"ilst",
               box(b"\xa9nam", box(b"data", struct.pack(">II", 1, 0) + "原始标题".encode("utf-8"))) +
               box(b"\xa9too", box(b"data", struct.pack(">II", 1, 0) + "TestEncoder".encode("utf-8"))) +
               extra_items)
    meta = box(b"meta", struct.pack(">I", 0) +
               box(b"hdlr", struct.pack(">I", 0) + b"\x00" * 4 + b"mdirappl" + b"\x00" * 9) + ilst)
    return box(b"udta", meta + keys_box)


def finalize(path, data):
    """写盘并把 stco 指向 mdat 数据起点（数据起点放 MAGIC 校验字节）。"""
    with open(path, "wb") as f:
        f.write(data)
    boxes = atoms.parse(data)
    stco = atoms.walk(boxes, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stco"])
    tops, _ = [], 0
    pos = 0
    mdat_start = None
    while pos + 8 <= len(data):
        size = struct.unpack_from(">I", data, pos)[0]
        t = data[pos + 4:pos + 8]
        if t == b"mdat":
            mdat_start = pos + 8
            break
        if size == 1:
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            pos += 16
        else:
            pos += max(size, 8)
    payload = bytearray(stco.payload)
    struct.pack_into(">I", payload, 8, mdat_start or 0)
    stco.payload = payload
    with open(path, "wb") as f:
        f.write(atoms.serialize(boxes))
    return path


def make_video(path, mb=0, moov_first=False, magic=True, extra_items=b"", keys_box=b""):
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")
    mdat_payload = (b"MDATMAGIC" + os.urandom(max(mb * 1024 * 1024, 4088) - 9)) if magic else b"\x00" * 1024
    mdat = box(b"mdat", mdat_payload)
    moov = box(b"moov", mvhd_of() + trak_of() + ilst_udta(extra_items, keys_box))
    data = (ftyp + moov + mdat) if moov_first else (ftyp + mdat + moov)
    return finalize(path, data)


def stco_points_at_magic(path):
    """校验 stco[0] 仍指向 mdat 数据的 MAGIC 字节 —— 视频未写坏的金标准。"""
    data = open(path, "rb").read()
    boxes = atoms.parse(data)
    stco = atoms.walk(boxes, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stco"])
    off = struct.unpack_from(">I", bytes(stco.payload), 8)[0]
    return data[off:off + 9] == b"MDATMAGIC"


def find_nested(path, t):
    """递归找第一个指定类型的 box，返回原始字节。"""
    data = open(path, "rb").read()

    def walk_boxes(buf):
        out = []
        pos = 0
        while pos + 8 <= len(buf):
            size = struct.unpack_from(">I", buf, pos)[0]
            if size < 8 or pos + size > len(buf):
                break
            btype = buf[pos + 4:pos + 8]
            payload = buf[pos + 8:pos + size]
            if btype == t:
                out.append(buf[pos:pos + size])
            if btype in (b"moov", b"udta", b"meta", b"ilst", b"trak"):
                # meta FullBox 有 4 字节头
                if btype == b"meta":
                    payload = payload[4:]
                out.extend(walk_boxes(payload))
            pos += size
        return out
    hits = walk_boxes(data)
    return hits[0] if hits else None


print("== 1. 基线：小文件 moov 在前 ==")
p1 = make_video(os.path.join(OUT, "t1_small_faststart.mp4"))
m1 = unified.read_video(p1)
hr("读取 ilst 标题", m1["ilst"].get("©nam", {}).get("value") == "原始标题")
hr("读取时长", "时长" in m1["basic"])

print("== 2. 大文件 moov 在尾部（60MB，手机视频典型布局） ==")
p2 = make_video(os.path.join(OUT, "t2_big_moov_end.mp4"), mb=60)
m2 = unified.read_video(p2)
hr("读取 ilst 标题", m2["ilst"].get("©nam", {}).get("value") == "原始标题")
hr("读取时长", "时长" in m2["basic"])
hr("读取轨道", len(m2["tracks"]) == 1 and m2["tracks"][0]["分辨率"] == "1920×1080")

print("== 3. 大文件写入（变长标题！验证 stco 不被写坏） ==")
w = unified.write_video(p2, changes={"©nam": "一个比原标题长很多的新标题XYZ"})
m3 = unified.read_video(p2)
hr(f"写入无异常 {w}", not w)
hr("回读到新标题", m3["ilst"].get("©nam", {}).get("value") == "一个比原标题长很多的新标题XYZ")
hr("stco 仍指向有效 mdat 数据（视频未损坏）", stco_points_at_magic(p2))

print("== 4. faststart 大文件写入（mdat 在 moov 后，offset 应平移） ==")
p4 = make_video(os.path.join(OUT, "t4_big_faststart.mp4"), mb=60, moov_first=True)
unified.write_video(p4, changes={"©nam": "长短不一的新标题!"})
hr("stco 仍指向有效 mdat 数据（视频未损坏）", stco_points_at_magic(p4))
m4 = unified.read_video(p4)
hr("回读到新标题", m4["ilst"].get("©nam", {}).get("value") == "长短不一的新标题!")

print("== 5. iPhone 风格 mdta Keys + 数字索引 ilst ==")
KEYS_GUID = uuidlib.UUID("a2394f52-5a9b-4f4a-a4c2-5b0e2c7b1e1f").bytes
keys_names = [b"com.apple.quicktime.make", b"com.apple.quicktime.model",
              b"com.apple.quicktime.software", b"com.apple.quicktime.creationdate",
              b"com.apple.quicktime.location.ISO6709"]
keys_payload = struct.pack(">I", 0) + struct.pack(">I", len(keys_names))
for n in keys_names:
    keys_payload += struct.pack(">I", len(n) + 8) + b"mdta" + n
# uuid box = 8字节头 + 16字节 user_type + payload
keys_box = (struct.pack(">I", 8 + 16 + len(keys_payload)) + b"uuid" + KEYS_GUID + keys_payload)
values = [b"Apple", b"iPhone 15 Pro", b"iOS 18.0", b"2026-10-01T12:00:00+0800",
          b"+31.2304+121.4737+005.500/"]
extra = b"".join(box(struct.pack(">I", i + 1), box(b"data", struct.pack(">II", 1, 0) + v))
                 for i, v in enumerate(values))
p5 = make_video(os.path.join(OUT, "t5_iphone.mp4"), extra_items=extra, keys_box=keys_box)
m5 = unified.read_video(p5)
hr("设备制造商读出", m5["ilst"].get("com.apple.quicktime.make", {}).get("value") == "Apple")
hr("设备型号读出", m5["ilst"].get("com.apple.quicktime.model", {}).get("value") == "iPhone 15 Pro")
hr("GPS 从 location.ISO6709 读出(含海拔)",
   m5.get("gps_decimal") == {"lat": 31.2304, "lon": 121.4737, "alt": 5.5})

print("== 6. GPS 写入 → 二次写入 → loci/©xyz 完好 ==")
p6 = make_video(os.path.join(OUT, "t6_gps.mp4"))
unified.write_video(p6, gps={"lat": 31.2304, "lon": 121.4737})
loci_before = find_nested(p6, b"loci")
unified.write_video(p6, changes={"©cmt": "第二次写入，长度不同"})
loci_after = find_nested(p6, b"loci")
m6 = unified.read_video(p6)
hr("二次写入后 ©xyz GPS 仍可读", m6.get("gps_decimal") == {"lat": 31.2304, "lon": 121.4737})
hr(f"loci 字节原样保留", loci_before == loci_after and loci_before is not None)
hr("stco 仍有效", stco_points_at_magic(p6))

print("== 7. 封面写入 → 提取（70MB moov 在尾，旧代码 64MB 截断读不到） ==")
from PIL import Image
buf = io.BytesIO()
Image.new("RGB", (320, 240), (255, 0, 0)).save(buf, "JPEG")
cover = buf.getvalue()
p7 = make_video(os.path.join(OUT, "t7_cover_big.mp4"), mb=70)
unified.write_video(p7, cover=cover)
got = unified.extract_cover(p7)
hr("提取到封面", got is not None and got[0][:2] == b"\xff\xd8")

print("== 8. 封面不被编辑保存毁掉（模拟前端把占位文本当值提交） ==")
unified.write_video(p7, changes={"covr": "<3600 字节 jpeg>", "©nam": "占位攻击"})
got8 = unified.extract_cover(p7)
hr("covr 文本写入被拒绝，封面无损", got8 is not None and got8[0][:2] == b"\xff\xd8")
m8 = unified.read_video(p7)
hr("标题正常写入", m8["ilst"].get("©nam", {}).get("value") == "占位攻击")

print("== 9. 隐私清理（iPhone 风格） ==")
p9 = make_video(os.path.join(OUT, "t9_privacy.mp4"), extra_items=extra, keys_box=keys_box)
privacy.scrub(p9, "geo")
m9 = unified.read_video(p9)
hr("geo：Keys 定位已清", "com.apple.quicktime.location.ISO6709" not in m9["ilst"])
hr("geo：设备信息保留", m9["ilst"].get("com.apple.quicktime.make", {}).get("value") == "Apple")
privacy.scrub(p9, "standard")
m9b = unified.read_video(p9)
hr("standard：Keys 设备痕迹全清", "com.apple.quicktime.model" not in m9b["ilst"])
hr("standard：©too 也被清", "©too" not in m9b["ilst"])
hr("stco 仍有效", stco_points_at_magic(p9))

print("== 10. 旧版(修复前)写入的裸 loci 兼容读取 ==")
p10 = make_video(os.path.join(OUT, "t10_legacy_loci.mp4"))
raw_loci = b"\x55\xc4\x00\x00" + struct.pack(">ii", int(31.2304 * 65536), int(121.4737 * 65536)) + b"\x00\x00\x00\x00"
data = open(p10, "rb").read()
boxes = atoms.parse(data)
ilst = atoms.walk(boxes, [b"moov", b"udta", b"meta", b"ilst"])
ilst.children.append(atoms.Box(type=b"loci", payload=bytearray(raw_loci)))
with open(p10, "wb") as f:
    f.write(atoms.serialize(boxes))
m10 = unified.read_video(p10)
g10 = m10.get("gps_decimal") or {}
hr(f"裸 loci(旧版) 读出 GPS ({g10})",
   abs(g10.get("lat", 0) - 31.2304) < 1e-4 and abs(g10.get("lon", 0) - 121.4737) < 1e-4)

print(f"\n{'全部通过' if all(RESULTS) else '存在失败'}: {sum(RESULTS)}/{len(RESULTS)}")
sys.exit(0 if all(RESULTS) else 1)
