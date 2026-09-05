"""Visual effects, canvas rendering primitives, and color manipulation for Downtime UI."""

from __future__ import annotations

import math
import tkinter as tk


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    """Convert a hex color string like '#1a2b3c' to an (r, g, b) tuple."""
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(c * 2 for c in color)
    if len(color) != 6:
        return 0, 0, 0
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert an (r, g, b) tuple (0-255) to a hex string."""
    r = max(0, min(255, int(r)))
    g = max(0, min(255, int(g)))
    b = max(0, min(255, int(b)))
    return f"#{r:02x}{g:02x}{b:02x}"


def blend_colors(color1: str, color2: str, ratio: float) -> str:
    """Linear interpolation between two hex colors.

    ratio=0.0 returns color1, ratio=1.0 returns color2.
    """
    ratio = max(0.0, min(1.0, float(ratio)))
    r1, g1, b1 = hex_to_rgb(color1)
    r2, g2, b2 = hex_to_rgb(color2)
    return rgb_to_hex(
        r1 + (r2 - r1) * ratio,
        g1 + (g2 - g1) * ratio,
        b1 + (b2 - b1) * ratio,
    )


def rounded_rectangle(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int = 8,
    *,
    fill: str = "",
    outline: str = "",
    width: int = 1,
    tags: str | tuple[str, ...] = (),
) -> int:
    """Draw a smooth rounded rectangle on a Tkinter Canvas using a smooth polygon."""
    x1, x2 = sorted((x1, x2))
    y1, y2 = sorted((y1, y2))
    radius = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)
    if radius <= 0:
        return canvas.create_rectangle(
            x1, y1, x2, y2, fill=fill, outline=outline, width=width, tags=tags
        )

    # 12-point smooth polygon geometry approximating rounded corners
    points = [
        x1 + radius, y1,
        x1 + radius, y1,
        x2 - radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(
        points,
        smooth=True,
        fill=fill,
        outline=outline,
        width=width,
        tags=tags,
    )


def draw_drop_shadow(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int = 8,
    layers: int = 3,
    shadow_base: str = "#040608",
    tags: str | tuple[str, ...] = ("shadow",),
) -> list[int]:
    """Draw multi-layered subtle ambient drop shadows beneath a card or modal."""
    shadow_ids = []
    for i in range(layers, 0, -1):
        offset = i
        spread = i
        shadow_id = rounded_rectangle(
            canvas,
            x1 - spread,
            y1 + offset,
            x2 + spread,
            y2 + offset + spread,
            radius=radius + spread,
            fill=shadow_base,
            outline="",
            tags=tags,
        )
        shadow_ids.append(shadow_id)
    return shadow_ids


def draw_glow_ring(
    canvas: tk.Canvas,
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    radius: int = 8,
    glow_color: str = "#35c6f4",
    glow_width: int = 2,
    tags: str | tuple[str, ...] = ("glow",),
) -> list[int]:
    """Draw a vibrant outer glow ring to indicate focus, active state, or selection."""
    glow_ids = []
    for i in range(glow_width, 0, -1):
        ring_id = rounded_rectangle(
            canvas,
            x1 - i,
            y1 - i,
            x2 + i,
            y2 + i,
            radius=radius + i,
            fill="",
            outline=glow_color,
            width=1,
            tags=tags,
        )
        glow_ids.append(ring_id)
    return glow_ids
