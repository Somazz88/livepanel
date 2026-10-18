"""Draw the app icon (dark rounded square, one rising green line) into livepanel/web/icon.ico.

Same picture as the page's favicon. Needs Pillow; only run when the icon changes.
    python scripts/make_icon.py
"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "livepanel" / "web" / "icon.ico"


def draw(size: int) -> Image.Image:
    s = 4                                                     # draw large, shrink: smooth edges
    big = size * s
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, big - 1, big - 1], radius=int(big * 14 / 64), fill=(17, 24, 39, 255))
    pts = [(10, 45), (24, 31), (34, 38), (54, 15)]
    pts = [(x * big / 64, y * big / 64) for x, y in pts]
    w = max(2, int(big * 7 / 64))
    d.line(pts, fill=(52, 199, 89, 255), width=w, joint="curve")
    for x, y in (pts[0], pts[-1]):                            # round caps
        d.ellipse([x - w / 2, y - w / 2, x + w / 2, y + w / 2], fill=(52, 199, 89, 255))
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    sizes = [16, 24, 32, 48, 64, 128, 256]
    draw(256).save(OUT, sizes=[(n, n) for n in sizes])
    print("wrote", OUT)
