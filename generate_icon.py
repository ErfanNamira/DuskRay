"""
generate_icon.py — creates duskray.ico from the same drawing code used by
the tray app, at multiple resolutions (Windows picks the best one per use).

Run:
    python generate_icon.py
"""

import math
from PIL import Image, ImageDraw


def make_icon_image(size, enabled=True):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (255, 150, 40, 255) if enabled else (130, 130, 130, 255)

    margin = size * 10 // 64
    d.ellipse((margin, margin, size - margin, size - margin), fill=color)

    if enabled:
        r_in = size * 24 // 64
        r_out = size * 30 // 64
        width = max(1, size * 3 // 64)
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            x1 = size / 2 + math.cos(rad) * r_in
            y1 = size / 2 + math.sin(rad) * r_in
            x2 = size / 2 + math.cos(rad) * r_out
            y2 = size / 2 + math.sin(rad) * r_out
            d.line((x1, y1, x2, y2), fill=color, width=width)
    return img


if __name__ == "__main__":
    sizes = [16, 24, 32, 48, 64, 128, 256]

    # Render each size natively (not just downscaled from 256px) so small
    # sizes like 16/24px stay crisp instead of blurry.
    images = [make_icon_image(s, enabled=True) for s in sizes]
    largest = images[-1]
    others = images[:-1]
    largest.save(
        "duskray.ico",
        format="ICO",
        append_images=others,
    )
    print("Saved duskray.ico with natively-rendered sizes:", sizes)