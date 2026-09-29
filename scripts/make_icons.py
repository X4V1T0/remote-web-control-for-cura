"""Generates the web app icons (development only; the PNGs are committed).

    uv run --no-project --python 3.12 --with pillow -- python scripts/make_icons.py

iOS needs PNG icons for the Home Screen (apple-touch-icon); SVG is not supported there.
"""

import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(__file__), "..", "RemoteWebControl", "web", "icons")
BACKGROUND = (24, 28, 36)
ORANGE = (255, 138, 38)
LIGHT = (235, 238, 243)


def icon(size: int, padding: float = 0.0) -> Image.Image:
    scale = 4  # Draw large and downsample for smooth edges.
    s = size * scale
    image = Image.new("RGBA", (s, s), BACKGROUND + (255,))
    draw = ImageDraw.Draw(image)
    inset = padding * s

    def box(x0, y0, x1, y1):
        span = s - 2 * inset
        return [inset + x0 * span, inset + y0 * span, inset + x1 * span, inset + y1 * span]

    # Printed part: three stacked layers, narrowing to the top.
    for i, (x0, x1) in enumerate([(0.22, 0.78), (0.28, 0.72), (0.34, 0.66)]):
        y1 = 0.80 - i * 0.115
        draw.rounded_rectangle(box(x0, y1 - 0.09, x1, y1), radius = 0.02 * s, fill = ORANGE)
    # Nozzle above the part.
    draw.polygon([tuple(box(0.40, 0.14, 0.60, 0.14)[:2]), tuple(box(0.60, 0.14, 0.60, 0.14)[:2]),
                  tuple(box(0.53, 0.33, 0.53, 0.33)[:2]), tuple(box(0.47, 0.33, 0.47, 0.33)[:2])], fill = LIGHT)
    draw.rectangle(box(0.36, 0.08, 0.64, 0.15), fill = LIGHT)
    # Build plate.
    draw.rounded_rectangle(box(0.12, 0.82, 0.88, 0.87), radius = 0.02 * s, fill = LIGHT)
    return image.resize((size, size), Image.LANCZOS)


def main() -> None:
    os.makedirs(OUT, exist_ok = True)
    icon(180).convert("RGB").save(os.path.join(OUT, "apple-touch-icon.png"))  # iOS ignores transparency.
    icon(192).save(os.path.join(OUT, "icon-192.png"))
    icon(512).save(os.path.join(OUT, "icon-512.png"))
    icon(512, padding = 0.1).save(os.path.join(OUT, "icon-maskable-512.png"))  # Android adaptive icons crop the edges.
    icon(32).save(os.path.join(OUT, "favicon-32.png"))
    print("Icons written to", os.path.abspath(OUT))


if __name__ == "__main__":
    main()
