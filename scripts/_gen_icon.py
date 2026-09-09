"""生成 icon.ico / icon.png / favicon.svg（世界树图标）。用完即删。"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "mdnotes" / "static"
OUT_ICO = STATIC / "icon.ico"
OUT_PNG = STATIC / "icon.png"
OUT_SVG = STATIC / "favicon.svg"
S = 512  # 高分辨率主图


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i : i + 2], 16) for i in (0, 2, 4))


def _lerp(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _vert_gradient(size, c0, c1):
    """返回整画布垂直渐变 RGBA 图。"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        d.line([(0, y), (size, y)], fill=(*_lerp(_hex(c0), _hex(c1), t), 255))
    return img


def _poly(img, d, pts, fill, k):
    """绘制缩放后的实心多边形。pts 为 0-64 坐标，k=size/64。"""
    d.polygon([(x * k, y * k) for x, y in pts], fill=(*_hex(fill), 255))


def draw(size: int) -> Image.Image:
    k = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # 1) 圆角背景（垂直渐变深蓝）
    bg = _vert_gradient(size, "#142246", "#08152a")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size, size], radius=int(0.14 * 64 * k), fill=255)
    bg.putalpha(mask)
    img.alpha_composite(bg)

    d = ImageDraw.Draw(img, "RGBA")

    # 2) 树冠光晕（同心半透明圆）
    for cx, cy, r, col, a in ((32, 27, 20, "#55e6cf", 42), (32, 27, 12, "#b9f9ec", 24)):
        d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=(*_hex(col), a))

    # 3) 树干（三色近似垂直渐变，从上到下三条带）
    _poly(img, d, [(30, 37), (34, 37), (33.2, 53), (30.8, 53)], "#9db0de", k)
    _poly(img, d, [(30, 30), (34, 30), (34, 38), (30, 38)], "#7c8fbd", k)
    _poly(img, d, [(30.4, 38), (33.6, 38), (33.2, 53), (30.8, 53)], "#465a8c", k)

    # 4) 三层树冠（实心层叠，形成立体感）
    _poly(img, d, [(32, 8), (22, 21), (42, 21)], "#a7f4e2", k)
    _poly(img, d, [(32, 14), (17, 34), (47, 34)], "#5fe9d0", k)
    _poly(img, d, [(32, 23), (21, 45), (43, 45)], "#3fdcc0", k)

    # 5) 核心光脉
    d.line([(32 * k, 23 * k), (32 * k, 47 * k)], fill=(*_hex("#d9fff6"), 160), width=max(2, int(1.6 * k)))

    return img


if __name__ == "__main__":
    img = draw(S)
    # 自检：中心区域应有青色（树冠）
    px = img.convert("RGBA").getpixel((S // 2, int(20 * (S / 64))))  # (256,160) 应在树冠
    print("self-check center(256,160) =", px)
    assert px[2] > px[0] and px[1] > 160, "树冠未绘制!"

    img.save(OUT_PNG)
    # 直接以 256px 主图为源，按尺寸列表输出所有帧，避免 append_images 只保留单帧的坑
    ico = img.resize((256, 256), Image.Resampling.LANCZOS)
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    ico.save(OUT_ICO, format="ICO", sizes=sizes)
    print("written:", OUT_ICO, OUT_PNG)