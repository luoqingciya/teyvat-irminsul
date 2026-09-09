"""图片压缩与处理工具。"""

import io
from pathlib import Path

from PIL import Image, ImageOps


def compress_image(data: bytes, max_width: int = 1920, quality: int = 85) -> bytes:
    """压缩图片：限制最大宽度、调整质量，保持原格式。

    - 动图 GIF / WebP 原样返回，避免损毁或扩展名与内容格式不符。
    - 自动按 EXIF 方向修正（手机照片常见旋转问题）。
    - 仅 PNG 保留 PNG，其余转为 JPEG。
    """
    if not data:
        return data
    img = Image.open(io.BytesIO(data))
    fmt = (img.format or "JPEG").upper()

    # 动图与 WebP 不做破坏性压缩：保持原始字节，避免动画丢失 / 格式错配
    if fmt == "GIF" and getattr(img, "is_animated", False):
        return data
    if fmt == "WEBP":
        return data

    # 修正 EXIF 方向
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    # 限制尺寸
    w, h = img.size
    if w > max_width:
        ratio = max_width / w
        new_size = (max_width, int(h * ratio))
        img = img.resize(new_size, Image.Resampling.LANCZOS)

    out = io.BytesIO()
    if fmt == "PNG":
        img.save(out, format="PNG", optimize=True)
    else:
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        img.save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def is_image(filename: str) -> bool:
    ext = Path(filename).suffix.lower()
    return ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
