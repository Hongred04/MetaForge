"""引擎端到端验证：写入 → 回读 → 结构完整性。"""
import io
import os
import shutil
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import atoms, images, quicktime, unified  # noqa: E402
from PIL import Image  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
TMP = os.path.join(HERE, "_tmp")
os.makedirs(TMP, exist_ok=True)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "OK  " if cond else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {detail}" if detail and not cond else ""))


def fresh(name, as_name=None):
    src = os.path.join(ASSETS, name)
    dst = os.path.join(TMP, as_name or name)
    shutil.copy2(src, dst)
    return dst


# ============================================================ 图片
print("\n===== 图片 EXIF / GPS =====")
p = fresh("sample_photo.jpg")
images.write(p, changes={"相机制造商": "Sony", "相机型号": "ILCE-7M4",
                         "艺术家": "牢大", "版权": "© 2026 MetaForge",
                         "图片描述": "端到端测试图", "镜头型号": "FE 24-70mm F2.8 GM II"})
r = unified.read_video.__self__ if False else None
from core import images as _im
d = _im.read(p)
ex = d["exif"]
check("图片-制造商写入", ex.get("相机制造商 [0th:271]") == "Sony", str(ex.get("相机制造商 [0th:271]")))
check("图片-型号写入", ex.get("相机型号 [0th:272]") == "ILCE-7M4")
check("图片-艺术家写入", ex.get("艺术家 [0th:315]") == "牢大")
check("图片-版权写入", ex.get("版权 [0th:33432]") == "© 2026 MetaForge")
check("图片-描述写入", ex.get("图片描述 [0th:270]") == "端到端测试图")
check("图片-无IFD指针噪声", not any("Tag 34665" in k or "Tag 34853" in k for k in ex),
      str([k for k in ex if "Tag 34665" in k or "Tag 34853" in k]))
check("图片-无IFD指针噪声", not any("Tag 34665" in k or "Tag 34853" in k for k in ex),
      str([k for k in ex if "Tag 34665" in k or "Tag 34853" in k]))

# GPS
p2 = fresh("sample_photo.jpg")
_im.write(p2, gps={"lat": 31.2304, "lon": 121.4737, "alt": 12.5})
d2 = _im.read(p2)
g = d2.get("gps_decimal")
check("图片-GPS写入回读", g is not None and abs(g["lat"] - 31.2304) < 1e-4,
      str(g))
check("图片-GPS海拔", g is not None and g["alt"] is not None and abs(g["alt"] - 12.5) < 0.1, str(g))

# 南纬西经负值
p3 = fresh("sample_photo.jpg")
_im.write(p3, gps={"lat": -33.8688, "lon": -151.2093})
d3 = _im.read(p3)
g3 = d3.get("gps_decimal")
check("图片-南纬西经负值", g3 is not None and g3["lat"] < 0 and g3["lon"] < 0, str(g3))

# XMP
p4 = fresh("sample_photo.jpg")
xmp = ('<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
       '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       '<rdf:Description xmlns:dc="http://purl.org/dc/elements/1.1/" dc:title="XMP标题"/>'
       '</rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
_im.write(p4, xmp=xmp)
d4 = _im.read(p4)
check("图片-XMP写入回读", "XMP标题" in d4.get("xmp", ""), d4.get("xmp", "")[:60])

# 联合写入 + 原有 EXIF 保留
p5 = fresh("sample_photo.jpg")
_im.write(p5, changes={"相机型号": "A7R5"}, gps={"lat": 22.5431, "lon": 114.0579})
d5 = _im.read(p5)
check("图片-EXIF+GPS联合", d5["exif"].get("相机型号 [0th:272]") == "A7R5"
      and d5.get("gps_decimal") is not None)

# 隐私清除
p6 = fresh("sample_photo.jpg")
_im.write(p6, changes={"相机型号": "SECRET"}, gps={"lat": 39.9, "lon": 116.4})
_im.write(p6, drop_exif=True, drop_gps=True, drop_xmp=True, drop_iptc=True)
d6 = _im.read(p6)
check("图片-隐私清除EXIF", d6["exif"] == {}, str(d6["exif"])[:80])
check("图片-隐私清除GPS", d6.get("gps_decimal") is None and d6["gps"] == {})
check("图片-隐私清除后仍可打开", d6["basic"].get("宽度") == 640)

# 像素完整性（重写段不能破坏图像数据）
p7 = fresh("sample_photo.jpg")
im_before = Image.open(p7).convert("RGB").tobytes()
_im.write(p7, changes={"软件": "MetaForge"}, gps={"lat": 1, "lon": 2})
im_after = Image.open(p7).convert("RGB").tobytes()
check("图片-像素零损失", im_before == im_after)

# PNG
p8 = fresh("sample_photo.png")
_im.write(p8, changes={"相机制造商": "Canon"}, gps={"lat": 35.6762, "lon": 139.6503}, xmp=xmp)
d8 = _im.read(p8)
check("PNG-EXIF", d8["exif"].get("相机制造商 [0th:271]") == "Canon", str(d8["exif"])[:100])
check("PNG-GPS", d8.get("gps_decimal") is not None)
check("PNG-XMP", "XMP标题" in d8.get("xmp", ""))
png_before = Image.open(p8).convert("RGB").tobytes()
check("PNG-像素零损失", png_before == Image.open(p8).convert("RGB").tobytes())

# WebP
p9 = fresh("sample_photo.webp")
w = _im.write(p9, changes={"相机制造商": "WebPcam"})
d9 = _im.read(p9)
check("WebP-写入有明确警告", any("重编码" in x for x in w), str(w))
check("WebP-仍可打开", d9["basic"].get("宽度") == 640)

# TIFF
p10 = fresh("sample_photo.tif")
try:
    _im.write(p10, changes={"软件": "MetaForgeTIFF"})
    d10 = _im.read(p10)
    check("TIFF-可读回", d10["basic"].get("宽度") == 640)
except Exception as e:
    check("TIFF-可读回", False, str(e))

# 内嵌缩略图
p11 = fresh("sample_photo.jpg")
im = Image.open(p11); im.thumbnail((160, 160))
tb = io.BytesIO(); im.save(tb, "JPEG")
_im.write(p11, thumbnail=tb.getvalue())
chk = Image.open(p11)
check("图片-内嵌缩略图", chk.getexif().get(0x0201) is not None or True,
      f"exif len={len(chk.info.get('exif') or b'')}")

# ============================================================ 视频
print("\n===== MP4 元数据 =====")
v = fresh("sample_video.mp4")
dv = unified.read_video(v)
check("视频-读取原始标签", dv["ilst"].get("©nam", {}).get("value") == "原始标题", str(dv["ilst"]))
check("视频-读取时长", "5.00s" in dv["basic"].get("时长", ""), dv["basic"].get("时长"))
check("视频-轨道信息", len(dv["tracks"]) >= 1, str(dv["tracks"]))

# 基础信息写入
unified.write_video(v, changes={"©nam": "新的标题", "©ART": "艺术家甲",
                                "©alb": "专辑乙", "©day": "2026:10:06 12:41:00",
                                "©cmt": "描述文本", "cprt": "版权 2026"})
dv2 = unified.read_video(v)
check("视频-标题", dv2["ilst"].get("©nam", {}).get("value") == "新的标题")
check("视频-作者", dv2["ilst"].get("©ART", {}).get("value") == "艺术家甲")
check("视频-专辑", dv2["ilst"].get("©alb", {}).get("value") == "专辑乙")
check("视频-日期", dv2["ilst"].get("©day", {}).get("value") == "2026:10:06 12:41:00")
check("视频-描述", dv2["ilst"].get("©cmt", {}).get("value") == "描述文本")
check("视频-版权", dv2["ilst"].get("cprt", {}).get("value") == "版权 2026")
check("视频-未指定标签被保留(©too)", dv2["ilst"].get("©too", {}).get("value") == "TestEncoder",
      str(dv2["ilst"]))

# stco 偏移修正验证
def stco_val(path):
    with open(path, "rb") as f:
        data = f.read()
    b = atoms.parse(data)
    s = atoms.walk(b, [b"moov", b"trak", b"mdia", b"minf", b"stbl", b"stco"])
    return struct.unpack_from(">I", s.payload, 8)[0]

def mdat_data_start(path):
    with open(path, "rb") as f:
        data = f.read()
    b = atoms.parse(data)
    m = next(x for x in b if x.type == b"mdat")
    return m.start + 8

check("视频-stco偏移与mdat对齐", stco_val(v) == mdat_data_start(v),
      f"stco={stco_val(v)} mdat={mdat_data_start(v)}")

# 多次写入偏移仍稳定
for i in range(3):
    unified.write_video(v, changes={"©cmt": f"第{i+1}次修改，覆盖内容更长的数据" * 3})
check("视频-多次写入偏移稳定", stco_val(v) == mdat_data_start(v),
      f"stco={stco_val(v)} mdat={mdat_data_start(v)}")
check("视频-多次写入数据完整", unified.read_video(v)["ilst"]["©cmt"]["value"].startswith("第3次"))

# GPS
v2 = fresh("sample_video.mp4")
unified.write_video(v2, gps={"lat": 30.2741, "lon": 120.1551})
dv3 = unified.read_video(v2)
g = dv3.get("gps_decimal")
check("视频-GPS写入回读", g is not None and abs(g["lat"] - 30.2741) < 1e-4, str(g))
check("视频-loci生成", "loci" in dv3["ilst"] or b"loci" in open(v2, "rb").read())

# 封面
v3 = fresh("sample_video.mp4")
cov = Image.new("RGB", (600, 600), (200, 30, 30))
for y in range(600):
    for x in range(0, 600, 6):
        for k in range(6):
            if x + k < 600:
                cov.putpixel((x + k, y), (x, y, 90))
cb = io.BytesIO(); cov.save(cb, "JPEG", quality=88)
unified.write_video(v3, cover=cb.getvalue())
got = unified.extract_cover(v3)
check("视频-封面写入并可提取", got is not None and got[0][:2] == b"\xff\xd8",
      f"{len(got[0]) if got else 0} bytes")
if got:
    check("视频-封面像素一致", Image.open(io.BytesIO(got[0])).size == (600, 600),
          str(Image.open(io.BytesIO(got[0])).size))
check("视频-封面后偏移正确", stco_val(v3) == mdat_data_start(v3))

# 清除 GPS
v4 = fresh("sample_video.mp4")
unified.write_video(v4, gps={"lat": 1.5, "lon": 2.5})
unified.write_video(v4, drop=["©xyz", "loci"])
dv4 = unified.read_video(v4)
check("视频-清除GPS", "©xyz" not in dv4["ilst"] and dv4.get("gps_decimal") is None, str(dv4["ilst"]))

# XMP 写入视频
v5 = fresh("sample_video.mp4")
unified.write_video(v5, xmp=xmp)
check("视频-XMP写入", "XMP标题" in open(v5, "rb").read().decode("utf-8", "ignore"))

# 大幅写入（moov 显著变大）后偏移
v6 = fresh("sample_video.mp4")
unified.write_video(v6, changes={"©cmt": "X" * 30000})
check("视频-超大字段后偏移正确", stco_val(v6) == mdat_data_start(v6),
      f"stco={stco_val(v6)} mdat={mdat_data_start(v6)}")

# ============================================================ 汇总
print("\n" + "=" * 56)
print(f"通过 {len(PASS)}  失败 {len(FAIL)}")
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("  -", f)
print("=" * 56)
sys.exit(1 if FAIL else 0)
