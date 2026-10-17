#!/usr/bin/env python3
"""Generate owned PWA PNGs directly from icon-source.svg geometry.

Requires Pillow (image-generation tool only, not an app runtime dependency).
No font, icon library, browser binary or network asset is used. MIT for this
project's owned code/assets; dependency licenses remain unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import math
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "app" / "static" / "icon-source.svg"
SUPERSAMPLE = 4
CURVE_STEPS = 64


def path_points(data: str) -> list[tuple[float, float]]:
    """Flatten the SVG's intentionally small absolute M/L/C/Z vocabulary."""
    tokens = re.findall(r"[MLCZ]|-?\d+(?:\.\d+)?", data)
    points: list[tuple[float, float]] = []
    pos = 0
    while pos < len(tokens):
        command = tokens[pos]
        pos += 1
        if command in ("M", "L"):
            points.append((float(tokens[pos]), float(tokens[pos + 1])))
            pos += 2
        elif command == "C":
            p0 = points[-1]
            x1, y1, x2, y2, x3, y3 = map(float, tokens[pos:pos + 6])
            pos += 6
            for step in range(1, CURVE_STEPS + 1):
                t = step / CURVE_STEPS
                q = 1 - t
                points.append((q**3*p0[0] + 3*q*q*t*x1 + 3*q*t*t*x2 + t**3*x3,
                               q**3*p0[1] + 3*q*q*t*y1 + 3*q*t*t*y2 + t**3*y3))
        elif command == "Z":
            points.append(points[0])
        else:
            raise ValueError(f"Unsupported SVG command: {command}")
    return points


def source_geometry():
    tree = ET.parse(SVG)
    elements = {element.attrib.get("id"): element for element in tree.getroot()}
    paper = elements["paper"].attrib["fill"]
    shape = elements["conversation"]
    mark = elements["done"]
    outline = path_points(shape.attrib["d"])
    # Maskable safe circle: center (256,256), radius 204.8 (80% diameter).
    radius = max(math.hypot(x - 256, y - 256) for x, y in outline)
    assert radius < 204.8, f"Main shape is outside maskable safe circle: {radius}"
    return paper, shape.attrib["fill"], outline, mark, radius


def render(size: int) -> Image.Image:
    paper, ink, outline, mark, _ = source_geometry()
    scale = size * SUPERSAMPLE / 512
    image = Image.new("RGB", (size * SUPERSAMPLE,) * 2, paper)
    drawing = ImageDraw.Draw(image)
    xy = [(round(x * scale), round(y * scale)) for x, y in outline]
    drawing.polygon(xy, fill=ink)
    line = [(round(x * scale), round(y * scale)) for x, y in path_points(mark.attrib["d"])]
    width = round(float(mark.attrib["stroke-width"]) * scale)
    color = mark.attrib["stroke"]
    drawing.line(line, fill=color, width=width, joint="curve")
    # Round caps and joins match the SVG stroke, including the middle vertex.
    radius = width / 2
    for x, y in line:
        drawing.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)
    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=SVG.parent)
    parser.add_argument("--preview", type=Path, help="Optional small-size review strip")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        destination = args.output_dir / f"icon-{size}.png"
        render(size).save(destination, format="PNG", optimize=False, compress_level=9)
        print(f"{destination.name}: {size}x{size} sha256={hashlib.sha256(destination.read_bytes()).hexdigest()}")
    print(f"maskable max geometry radius={source_geometry()[-1]:.2f}; limit=204.8")
    if args.preview:
        paper = source_geometry()[0]
        strip = Image.new("RGB", (300, 96), paper)
        x = 20
        for size in (16, 24, 32, 48):
            strip.paste(render(size), (x, (96-size)//2))
            x += size + 32
        strip.save(args.preview, format="PNG")
        print(f"preview native sizes left-to-right: 16,24,32,48 -> {args.preview}")


if __name__ == "__main__":
    main()
