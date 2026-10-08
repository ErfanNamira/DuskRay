"""
generate_icon.py — creates duskray.ico from the exact artwork used by the tray
icon (duskray.render_icon), rendered natively at every size so small sizes
stay crisp instead of being blurry downscales.

Run:
    python generate_icon.py
"""
from duskray import render_icon

SIZES = (16, 24, 32, 48, 64, 128, 256)

if __name__ == "__main__":
    images = [render_icon("active", s) for s in SIZES]
    images[-1].save("duskray.ico", format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=images[:-1])
    print("Saved duskray.ico with natively-rendered sizes:", list(SIZES))
