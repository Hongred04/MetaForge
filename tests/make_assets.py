"""生成测试媒体文件：真实可读的 MP4（带 stco/ilst）与各类图片。"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import atoms, quicktime  # noqa: E402
from PIL import Image  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
os.makedirs(OUT, exist_ok=True)


def box(t, payload):
    return struct.pack(">I", len(payload) + 8) + t + payload


def make_mp4(path):
    """构造一个结构完整、最小可解析的 MP4：ftyp + moov(mvhd/trak/udta/meta/ilst) + mdat"""
    timescale = 1000
    duration = 5000

    # ---- mdat：1 帧假数据 ----
    mdat_payload = b"\x00" * 1024
    ftyp = box(b"ftyp", b"isom" + struct.pack(">I", 512) + b"isomiso2avc1mp41")

    # ---- mvhd (version 0) ----
    mvhd_payload = struct.pack(">I", 0)            # version/flags
    mvhd_payload += struct.pack(">IIII", 0, 0, timescale, duration)
    mvhd_payload += struct.pack(">i", 0x00010000)  # rate
    mvhd_payload += struct.pack(">h", 0x0100)      # volume
    mvhd_payload += b"\x00" * 10
    mvhd_payload += struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    mvhd_payload += b"\x00" * 24                   # predefined
    mvhd_payload += struct.pack(">I", 2)           # next track id
    mvhd = box(b"mvhd", mvhd_payload)

    # ---- tkhd (version 0, flags=7 enabled) ----
    tkhd_payload = struct.pack(">I", 7)
    tkhd_payload += struct.pack(">IIIII", 0, 0, 1, 0, duration)
    tkhd_payload += b"\x00" * 8
    tkhd_payload += struct.pack(">hhhh", 0, 0, 0, 0)
    tkhd_payload += struct.pack(">9i", 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)
    tkhd_payload += struct.pack(">II", 640 << 16, 480 << 16)
    tkhd = box(b"tkhd", tkhd_payload)

    hdlr = box(b"hdlr", struct.pack(">I", 0) + b"\x00" * 4 + b"vide" + b"\x00" * 12 + b"VideoHandler\x00")
    vmhd = box(b"vmhd", struct.pack(">I", 1) + struct.pack(">HHHH", 0, 0, 0, 0))
    dref = box(b"dref", struct.pack(">I", 0) + struct.pack(">I", 1) + box(b"url ", struct.pack(">I", 1)))
    dinf = box(b"dinf", dref)
    stbl = box(b"stbl",
               box(b"stsd", struct.pack(">I", 0) + struct.pack(">I", 1) + box(b"avc1", b"\x00" * 78)) +
               box(b"stts", struct.pack(">I", 0) + struct.pack(">I", 0)) +
               box(b"stsc", struct.pack(">I", 0) + struct.pack(">I", 0)) +
               box(b"stsz", struct.pack(">I", 0) + struct.pack(">II", 0, 0)) +
               # 关键：stco 指向 mdat 数据起点，写作时会被自动修正
               box(b"stco", struct.pack(">I", 0) + struct.pack(">I", 1) + struct.pack(">I", 0)))
    minf = box(b"minf", vmhd + dinf + stbl)
    mdhd = box(b"mdhd", struct.pack(">I", 0) + struct.pack(">IIII", 0, 0, timescale, duration) + struct.pack(">HH", 0x55C4, 0))
    mdia = box(b"mdia", mdhd + hdlr + minf)
    trak = box(b"trak", tkhd + mdia)

    # ---- udta/meta/ilst：预置一些厂商痕迹，方便验证"保留" ----
    ilst = box(b"ilst",
               box(b"\xa9nam", box(b"data", struct.pack(">II", 1, 0) + "原始标题".encode())) +
               box(b"\xa9too", box(b"data", struct.pack(">II", 1, 0) + "TestEncoder".encode())))
    meta = box(b"meta", struct.pack(">I", 0) +
               box(b"hdlr", struct.pack(">I", 0) + b"\x00" * 4 + b"mdirappl" + b"\x00" * 9) +
               ilst)
    udta = box(b"udta", meta)

    moov = box(b"moov", mvhd + trak + udta)
    mdat = box(b"mdat", mdat_payload)
    data = ftyp + moov + mdat

    with open(path, "wb") as f:
        f.write(data)
    # 修正 stco 指向真实 mdat 数据起点
    boxes = atoms.parse(data)
    stco = atoms.walk(boxes, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stco"])
    mdat_start = len(data) - len(mdat_payload)
    payload = bytearray(stco.payload)
    struct.pack_into(">I", payload, 8, mdat_start)
    stco.payload = payload
    with open(path, "wb") as f:
        f.write(atoms.serialize(boxes))
    return path


def make_images():
    paths = {}
    specs = {
        "sample_photo.jpg": ("JPEG", "photo"),
        "sample_photo.png": ("PNG", "photo"),
        "sample_photo.webp": ("WEBP", "photo"),
        "sample_photo.tif": ("TIFF", "photo"),
    }
    for name, (fmt, mode) in specs.items():
        p = os.path.join(OUT, name)
        im = Image.new("RGB", (640, 480))
        px = im.load()
        for y in range(480):
            for x in range(0, 640, 8):
                for k in range(8):
                    if x + k < 640:
                        px[x + k, y] = ((x + k) % 256, (y * 2) % 256, 160)
        im.save(p, fmt)
        paths[name] = p
    return paths


if __name__ == "__main__":
    v = make_mp4(os.path.join(OUT, "sample_video.mp4"))
    print("video:", v, os.path.getsize(v), "bytes")
    imgs = make_images()
    for k, p in imgs.items():
        print("image:", k, os.path.getsize(p), "bytes")
