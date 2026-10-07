"""
隐私清除策略：按预设批量抹除痕迹。

预设说明:
  standard —— 发朋友圈/社交媒体前的安全清理（去定位、去设备、留艺术信息）
  strict  —— 完全归零，只留尺寸等无 identifying 信息的字段
  geo     —— 只清 GPS，保留拍摄设备信息
"""

from __future__ import annotations

from . import images as _images
from . import atoms, quicktime, unified

# EXIF 0th/Exif IFD 中属于"设备痕迹"的标签
DEVICE_TAGS_0TH = [
    0x010F,  # Make
    0x0110,  # Model
    0x0131,  # Software
    0x0132,  # DateTime
    0x013B,  # Artist
    0x013B,
    0x8298,  # Copyright
    0x010E,  # ImageDescription
    0x9C9B,  # XP Title
    0x9C9C,  # XP Comment
    0x9C9D,  # XP Author
    0x9C9E,  # XP Keywords
    0x9C9F,  # XP Subject
    0x0213,  # YCbCrPositioning
    0x0214,  # ReferenceBlackWhite
]

DEVICE_TAGS_EXIF = [
    0x9000,  # ExifVersion (保留)
    0x9003,  # DateTimeOriginal
    0x9004,  # DateTimeDigitized
    0x9010,  # OffsetTime
    0x9011,  # OffsetTimeOriginal
    0x9012,  # OffsetTimeDigitized
    0x9101,  # ComponentsConfiguration
    0x9102,  # CompressedBitsPerPixel
    0x9201,  # ShutterSpeedValue
    0x9202,  # ApertureValue
    0x9203,  # BrightnessValue
    0x9204,  # ExposureBiasValue
    0x9205,  # MaxApertureValue
    0x9206,  # SubjectDistance
    0x9207,  # MeteringMode
    0x9208,  # LightSource
    0x9209,  # Flash
    0x920A,  # FocalLength
    0x927C,  # MakerNote
    0x9286,  # UserComment
    0x9290,  # SubSecTime
    0x9291,  # SubSecTimeOriginal
    0x9292,  # SubSecTimeDigitized
    0xA000,  # FlashpixVersion
    0xA001,  # ColorSpace
    0xA002,  # PixelXDimension (保留尺寸)
    0xA003,  # PixelYDimension (保留尺寸)
    0xA004,  # RelatedSoundFile
    0xA20B,  # FlashEnergy
    0xA20C,  # SpatialFrequencyResponse
    0xA20E,  # FocalPlaneXResolution
    0xA20F,  # FocalPlaneYResolution
    0xA210,  # FocalPlaneResolutionUnit
    0xA214,  # SubjectLocation
    0xA215,  # ExposureIndex
    0xA217,  # SensingMethod
    0xA300,  # FileSource
    0xA301,  # SceneType
    0xA302,  # CFAPattern
    0xA401,  # CustomRendered
    0xA402,  # ExposureMode
    0xA403,  # WhiteBalance
    0xA404,  # DigitalZoomRatio
    0xA405,  # FocalLengthIn35mmFilm
    0xA406,  # SceneCaptureType
    0xA407,  # GainControl
    0xA408,  # Contrast
    0xA409,  # Saturation
    0xA40A,  # Sharpness
    0xA40C,  # SubjectDistanceRange
    0xA420,  # ImageUniqueID
    0xA430,  # CameraOwnerName
    0xA431,  # BodySerialNumber
    0xA432,  # LensSpecification
    0xA433,  # LensMake
    0xA434,  # LensModel
    0xA435,  # LensSerialNumber
    0xA500,  # Gamma
]

# 尺寸类字段：任何策略下都保留，否则文件失去意义
KEEP_TAGS = {0xA002, 0xA003, 0x0100, 0x0101, 0x0102, 0x0103, 0x0112, 0x011A, 0x0132}

VIDEO_DROP_STANDARD = ["©xyz", "loci", "desc", "auth", "cprt", "©too", "©enc"]
VIDEO_DROP_STRICT = ["©nam", "©ART", "©alb", "©cmt", "©day", "©gen", "©too",
                     "©enc", "cprt", "auth", "desc", "ldes", "©xyz", "loci",
                     "trkn", "disk", "rate", "keyw", "covr", "stik", "pgap"]
VIDEO_DROP_GEO = ["©xyz", "loci"]

XMP_STRIP_KEYS_STRICT = [
    "dc:creator", "dc:title", "dc:description", "dc:rights", "dc:subject",
    "xmp:CreatorTool", "xmp:CreateDate", "xmp:ModifyDate", "xmp:MetadataDate",
    "xmp:Rating", "photoshop:DateCreated", "tiff:Make", "tiff:Model",
    "aux:SerialNumber", "aux:Lens", "aux:LensSerialNumber", "aux:Firmware",
    "exifEXIF:DateTimeOriginal", "exifEXIF:LensModel",
]

XMP_STRIP_KEYS_GEO = ["exif:GPSLatitude", "exif:GPSLongitude", "exif:GPSAltitude",
                      "exif:GPSLatitudeRef", "exif:GPSLongitudeRef",
                      "photoshop:Location", "exifEXIF:GPS*"]


def scrub_image(path: str, preset: str = "standard") -> list[str]:
    warnings: list[str] = []
    with open(path, "rb") as f:
        data = f.read()

    d = _images.load_exif_dict(data)

    if preset == "strict":
        keep0 = {t: v for t, v in (d.get("0th") or {}).items()
                 if t in (0x0100, 0x0101, 0x0102, 0x0103, 0x0112, 0x011A)}
        keepex = {t: v for t, v in (d.get("Exif") or {}).items()
                  if t in (0xA002, 0xA003, 0xA001, 0x9101)}
        d["0th"] = keep0
        d["Exif"] = keepex
        d["GPS"] = {}
        d["Interop"] = {}
        d["1st"] = {}
        d["thumbnail"] = None
    elif preset == "geo":
        d["GPS"] = {}
    else:  # standard
        d["0th"] = {t: v for t, v in (d.get("0th") or {}).items()
                    if t in KEEP_TAGS or t in (0x0112, 0x011A, 0x0103)}
        d["Exif"] = {t: v for t, v in (d.get("Exif") or {}).items()
                     if t in KEEP_TAGS or t in (0xA002, 0xA003, 0xA001)}
        d["GPS"] = {}
        d["Interop"] = {}
        d["thumbnail"] = None

    xmp_out = ""
    if preset == "strict":
        xmp_out = ""
    elif preset == "geo":
        xmp_out = _strip_xmp_keys(_images._extract_xmp_raw(path), XMP_STRIP_KEYS_GEO)
    else:
        xmp_out = _strip_xmp_keys(_images._extract_xmp_raw(path), XMP_STRIP_KEYS_STRICT)

    import piexif
    try:
        exif = piexif.dump(d)
        if exif.startswith(b"Exif\x00\x00"):
            exif = exif[6:]
    except Exception as e:
        warnings.append(f"EXIF 重建失败: {e}")
        exif = b""

    ext = path.lower().rsplit(".", 1)[-1]
    if ext in ("jpg", "jpeg", "jpe"):
        new = _images._jpeg_set_segments(data, exif=exif, xmp=xmp_out, drop=("iptc",))
    elif ext == "png":
        new = data
        if exif:
            new = _images._png_set_exif(new, exif)
        new = _images._png_set_xmp(new, xmp_out)
    else:
        new = data
        warnings.append(f"{ext.upper()} 需专用处理，已仅支持读取")

    if new != data:
        with open(path, "wb") as f:
            f.write(new)
    return warnings


def _strip_xmp_keys(xmp: str, keys: list[str]) -> str:
    """从 XMP 文本中移除含指定属性的元素（正则层面，尽量保守）。"""
    import re
    if not xmp or not xmp.strip():
        return ""
    out = xmp
    for k in keys:
        if k.endswith("*"):
            base = k[:-1]
            pattern = re.compile(
                rf'<(ns\d+:|[A-Za-z]+:)?{re.escape(base)}[^>]*/>', re.IGNORECASE)
            out = pattern.sub("", out)
            # rdf:Description 形式
            pattern2 = re.compile(
                rf'\s{re.escape(base)}[^=]*="[^"]*"', re.IGNORECASE)
            out = pattern2.sub("", out)
        else:
            out = re.sub(rf'<[^>]*\s{re.escape(k)}="[^"]*"[^>]*/?>', "", out, flags=re.IGNORECASE)
            out = re.sub(rf'\s{re.escape(k)}="[^"]*"', "", out, flags=re.IGNORECASE)
    return out


def scrub_video(path: str, preset: str = "standard") -> list[str]:
    if preset == "strict":
        drop = VIDEO_DROP_STRICT
    elif preset == "geo":
        drop = VIDEO_DROP_GEO
    else:
        drop = VIDEO_DROP_STANDARD

    # iPhone/安卓视频的设备痕迹（厂商/型号/系统/定位）在 mdta Keys 体系里：
    # standard/strict 整个 Keys box 连同序号项删除；geo 只删定位类 key
    drop_keys_all = preset in ("standard", "strict")
    drop_keys = ["*location*", "*Location*"] if preset == "geo" else None

    # geo 档只去定位：XMP 保留非定位字段；standard/strict 直接清空视频 XMP
    if preset == "geo":
        boxes, _brand, _total, _names = unified._load_moov(path)
        xmp_out = _strip_xmp_keys(quicktime.read_ilst(boxes)["xmp"],
                                  XMP_STRIP_KEYS_GEO)
    else:
        xmp_out = ""

    try:
        # 流式写入：大视频不再整体读入内存
        return unified.write_video(path, drop=drop, xmp=xmp_out,
                                   drop_keys=drop_keys, drop_keys_all=drop_keys_all)
    except Exception as e:
        return [f"隐私清除失败: {e}"]


def scrub(path: str, preset: str = "standard") -> list[str]:
    k = unified.kind_of(path)
    if k == "image":
        return scrub_image(path, preset)
    if k == "video":
        return scrub_video(path, preset)
    return [f"不支持的文件类型: {path}"]
