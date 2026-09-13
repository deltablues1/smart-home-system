"""Measure what the moon path actually draws.

The question is which SVG sweep flag produces a gibbous and which a crescent.
Rather than reason about it, this implements the W3C endpoint-to-centre arc
conversion (SVG 1.1 F.6.5), samples both arcs into a polygon, fills it, and
counts pixels. The measured lit fraction should equal the astronomical one.
"""

import math

from PIL import Image, ImageDraw

R = 30.0
BOX = 84.0
C = BOX / 2
SCALE = 12  # supersample so the count is not dominated by edge pixels


def arc_points(x1, y1, x2, y2, rx, ry, large, sweep, steps=900):
    """W3C SVG 1.1 F.6.5: endpoint parameterisation -> centre parameterisation."""
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    rx, ry = abs(rx), abs(ry)

    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p, y1p = dx2, dy2  # no rotation in our path

    # Scale the radii up if they are too small to span the endpoints.
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)

    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    factor = math.sqrt(max(0.0, num / den))
    if large == sweep:
        factor = -factor
    cxp = factor * rx * y1p / ry
    cyp = -factor * ry * x1p / rx

    cx = cxp + (x1 + x2) / 2.0
    cy = cyp + (y1 + y2) / 2.0

    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        n = math.hypot(ux, uy) * math.hypot(vx, vy)
        a = math.acos(max(-1.0, min(1.0, dot / n)))
        return -a if (ux * vy - uy * vx) < 0 else a

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                  (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if sweep == 0 and delta > 0:
        delta -= 2 * math.pi
    elif sweep == 1 and delta < 0:
        delta += 2 * math.pi

    return [
        (cx + rx * math.cos(theta1 + delta * i / steps),
         cy + ry * math.sin(theta1 + delta * i / steps))
        for i in range(steps + 1)
    ]


def lit_fraction(fraction, sweep):
    """Fill the path the module builds and measure how much of the disc is lit."""
    rx = abs(R * (1 - 2 * fraction))
    top = (C, C - R)
    bottom = (C, C + R)
    points = [top]
    points += arc_points(*top, *bottom, R, R, 0, 1)
    points += arc_points(*bottom, *top, rx, R, 0, sweep)

    size = int(BOX * SCALE)
    img = Image.new("1", (size, size), 0)
    ImageDraw.Draw(img).polygon([(x * SCALE, y * SCALE) for x, y in points], fill=1)
    lit = sum(img.getdata())
    disc = math.pi * (R * SCALE) ** 2
    return lit / disc


print("  mjerena osvijetljenost nacrtanog lika\n")
print(f"  {'stvarno':>9}   {'sweep=0':>9}   {'sweep=1':>9}   ispravan izbor")
for f in (0.05, 0.20, 0.50, 0.83, 0.95):
    a = lit_fraction(f, 0)
    b = lit_fraction(f, 1)
    best = "sweep=0" if abs(a - f) < abs(b - f) else "sweep=1"
    print(f"  {f:9.2f}   {a:9.3f}   {b:9.3f}   {best}")

print()
print("  s pravilom iz koda (sweep = f < 0.5 ? 0 : 1):")
print(f"  {'stvarno':>9}   {'nacrtano':>9}   {'razlika':>8}")
worst = 0.0
for f in (0.02, 0.10, 0.25, 0.40, 0.50, 0.60, 0.75, 0.83, 0.92, 0.99):
    drawn = lit_fraction(f, 0 if f < 0.5 else 1)
    worst = max(worst, abs(drawn - f))
    print(f"  {f:9.2f}   {drawn:9.3f}   {drawn - f:+8.3f}")
print(f"\n  najveće odstupanje: {worst:.3f}")
