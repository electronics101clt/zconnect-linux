"""Panel icon rendering.

Icons are generated at runtime with cairo into a cache dir, so there are no
binary assets in the repo. Each connection state gets its own colour so the
state is readable at panel size (22px) without hovering.
"""

import os

import cairo

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "zconnect", "icons",
)

# state -> (fill colour, glyph colour)
STATE_COLORS = {
    "connected":  ((0.18, 0.80, 0.44), (1, 1, 1)),
    "connecting": ((0.95, 0.61, 0.07), (1, 1, 1)),
    "scanning":   ((0.20, 0.60, 0.86), (1, 1, 1)),
    "idle":       ((0.45, 0.49, 0.51), (1, 1, 1)),
    "error":      ((0.91, 0.30, 0.24), (1, 1, 1)),
}

SIZE = 64


def _rounded_rect(ctx, x, y, w, h, r):
    import math
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    ctx.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    ctx.close_path()


def _draw(path, fill, glyph):
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, SIZE, SIZE)
    ctx = cairo.Context(surface)

    pad = 4
    _rounded_rect(ctx, pad, pad, SIZE - 2 * pad, SIZE - 2 * pad, 14)
    ctx.set_source_rgb(*fill)
    ctx.fill()

    # A bold "Z" drawn as a path — no font dependency, crisp when scaled down.
    ctx.set_source_rgb(*glyph)
    ctx.set_line_width(7)
    ctx.set_line_cap(cairo.LINE_CAP_BUTT)
    ctx.set_line_join(cairo.LINE_JOIN_MITER)
    left, right, top, bottom = 20, 44, 21, 43
    ctx.move_to(left, top)
    ctx.line_to(right, top)
    ctx.line_to(left, bottom)
    ctx.line_to(right, bottom)
    ctx.stroke()

    surface.write_to_png(path)


def ensure_icons():
    """Render every state icon into the cache dir. Returns the dir path."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    for state, (fill, glyph) in STATE_COLORS.items():
        path = os.path.join(CACHE_DIR, icon_name(state) + ".png")
        if not os.path.exists(path):
            _draw(path, fill, glyph)
    return CACHE_DIR


def icon_name(state):
    return "zconnect-" + (state if state in STATE_COLORS else "idle")


def icon_path(state):
    return os.path.join(CACHE_DIR, icon_name(state) + ".png")


if __name__ == "__main__":
    print(ensure_icons())
