"""Generate the app icon (.ico) and extension icons (PNG) from one design.

    python packaging/make_icons.py
"""

import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EXT = os.path.join(ROOT, "browser-extension", "icons")

GOLD = (242, 193, 78, 255)
DARK = (58, 44, 11, 255)


def draw(size):
    """Gold rounded tile with three dark bars (the brand mark), rendered at 4x."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = s * 0.10
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s * 0.22, fill=GOLD)
    bar_h = s * 0.07
    left = s * 0.30
    for i, cy in enumerate((0.40, 0.52, 0.64)):
        y = s * cy
        right = s * (0.70 - i * 0.09)
        d.rounded_rectangle([left, y, right, y + bar_h], radius=bar_h / 2, fill=DARK)
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(EXT, exist_ok=True)
    # App icon: multi-size .ico
    ico_path = os.path.join(HERE, "icon.ico")
    draw(256).save(ico_path, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote", ico_path)
    # Extension icons
    for sz in (16, 48, 128):
        p = os.path.join(EXT, f"icon{sz}.png")
        draw(sz).save(p)
        print("wrote", p)
    # 512px master for building a macOS .icns
    p512 = os.path.join(HERE, "icon-512.png")
    draw(512).save(p512)
    print("wrote", p512)


if __name__ == "__main__":
    main()
