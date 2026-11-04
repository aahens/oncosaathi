"""Walkthrough map: draws the route line on the floor plans, from where the patient is standing to the room.

A place is a hospital.json unit id, GATE1, GATE2, or LIFT-<floor>. Every line runs along the main corridor and enters the
room at the door side of the box make_floor_plans.py drew, so the map can't disagree with hospital.json. A trip across floors
gives two panels: start floor to the main lift, then the main lift to the room. The image is the same for web chat and
WhatsApp (a JPEG at a URL), and holds nothing about the patient.
"""
import functools
import io
import math
import os

from PIL import Image, ImageDraw, ImageFont

import make_floor_plans as plans

SCALE = 2                                                # floor_plans/*.png are drawn at twice the plan's coordinates
CORRIDOR_Y = (plans.CORR0 + plans.CORR1) / 2
LIFT_X = (plans.WINGS[1][1] + plans.WINGS[1][2]) / 2
LIFT_DOOR_Y = plans.CORR0 - 20
GATE_X = {"GATE1": plans.BX0 - 48, "GATE2": plans.WINGS[-1][2] + 8}
UNITS = {u["id"]: u for u in plans.H["units"]}
FLOOR_NAMES = {f["id"]: f["name"] for f in plans.H["floors"]}
BOXES = {u["id"]: (x, y, w, h) for fid in plans.ORDER for u, x, y, w, h in plans.room_boxes(fid)}
ROUTE, CASING, START, GOAL, NAVY = "#0B57D0", "#FFFFFF", "#C62828", "#1B7A4C", "#1E2B5C"
OUT_WIDTH = 1600
MAP_VERSION = 2                                          # raise when the drawing changes, so browsers fetch the new image
FONTS = ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")


def is_place(place):
    if place in GATE_X:
        return True
    if place and place.startswith("LIFT-"):
        return place[5:] in FLOOR_NAMES
    return place in UNITS and UNITS[place].get("public") is not False and "floor" in UNITS[place]


def floor_of(place):
    if place in GATE_X:
        return "G"
    return place[5:] if place.startswith("LIFT-") else UNITS[place]["floor"]


def door(place):
    """(door point, where that door meets the corridor) in plan coordinates."""
    if place in GATE_X:
        return (GATE_X[place], CORRIDOR_Y), GATE_X[place]
    if place.startswith("LIFT-"):
        return (LIFT_X, LIFT_DOOR_Y), LIFT_X
    x, y, w, h = BOXES[place]
    cx = x + w / 2
    return (cx, y + h if y < plans.CORR0 else y), cx


def line(a, b):
    """The walk between two places on one floor: out of the door, along the corridor, in at the other door."""
    (pa, xa), (pb, xb) = door(a), door(b)
    pts = [pa, (xa, CORRIDOR_Y), (xb, CORRIDOR_Y), pb]
    return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]


def legs(start, dest):
    """[{"floor", "from", "to", "points"}]: one leg per floor. Empty if they are already there."""
    if start == dest:
        return []
    fa, fb = floor_of(start), floor_of(dest)
    if fa == fb:
        return [{"floor": fa, "from": start, "to": dest, "points": line(start, dest)}]
    out = []
    if start != f"LIFT-{fa}":
        out.append({"floor": fa, "from": start, "to": f"LIFT-{fa}", "points": line(start, f"LIFT-{fa}")})
    if dest != f"LIFT-{fb}":
        out.append({"floor": fb, "from": f"LIFT-{fb}", "to": dest, "points": line(f"LIFT-{fb}", dest)})
    return out


def side(start, dest):
    """Which hand the room is on at the end of the walk: 'left', 'right' or 'ahead'. North is at the top of the plan."""
    if dest not in BOXES:
        return "ahead"
    leg = legs(start, dest)[-1]
    (_, xa), (_, xb) = door(leg["from"]), door(dest)
    if xa == xb or BOXES[dest][1] >= plans.CORR1 and UNITS[dest]["block"] == "CORE" and abs(xa - xb) < 1:
        return "ahead"
    north = BOXES[dest][1] < plans.CORR0
    return "left" if (xb > xa) == north else "right"


def _font(size):
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _name(place):
    if place in GATE_X:
        return "Gate 1" if place == "GATE1" else "Gate 2"
    return "main lift" if place.startswith("LIFT-") else UNITS[place]["name"]


def _pill(d, text, center, color, width_limit):
    font = _font(50)
    left, top, right, bottom = d.textbbox((0, 0), text, font=font)
    w, h = right - left + 50, bottom - top + 34
    x = min(max(center[0] - w / 2, 8), width_limit - w - 8)
    y = center[1] - h / 2
    d.rounded_rectangle((x, y, x + w, y + h), radius=20, fill=color, outline="#FFFFFF", width=6)
    d.text((x + w / 2, y + h / 2), text, font=font, fill="#FFFFFF", anchor="mm")


def _arrow(d, at, angle):
    size = 54
    pts = [(at[0] + size * math.cos(angle + a), at[1] + size * math.sin(angle + a)) for a in (0, 2.3, -2.3)]
    d.polygon(pts, fill=NAVY, outline="#FFFFFF", width=7)


def _panel(leg, title, start_label, goal_label):
    """One floor: the plan cropped to the building, with the route, a start dot and the highlighted goal."""
    fid = leg["floor"]
    x0, x1 = 10, plans.W - 10                            # the whole floor, so nothing is cut off
    y0, y1 = plans.BY0 - 62, plans.BY1 + (72 if fid == "G" else 10)
    with Image.open(os.path.join(plans.OUT, f"floor_{fid}.png")) as base:
        img = base.convert("RGB").crop((x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE))
    d = ImageDraw.Draw(img)
    to_px = lambda p: ((p[0] - x0) * SCALE, (p[1] - y0) * SCALE)
    pts = [to_px(p) for p in leg["points"]]
    if leg["to"] in BOXES:
        x, y, w, h = BOXES[leg["to"]]
        (ax, ay), (bx, by) = to_px((x, y)), to_px((x + w, y + h))
        d.rounded_rectangle((ax - 4, ay - 4, bx + 4, by + 4), radius=14, outline=GOAL, width=14)
    d.line(pts, fill=CASING, width=44, joint="curve")
    d.line(pts, fill=ROUTE, width=30, joint="curve")
    for a, b in zip(pts, pts[1:]):                       # arrowheads show which way to walk
        length, angle = math.dist(a, b), math.atan2(b[1] - a[1], b[0] - a[0])
        if length < 200:                                 # no room for one on the short step out of a door
            continue
        for i in range(max(1, int(length // 420))):
            t = (i + 0.5) / max(1, int(length // 420))
            _arrow(d, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), angle)

    def dot(point, color):                               # sits in the corridor side of the door, clear of the room number
        cx, cy = point
        if abs(cy - to_px((0, CORRIDOR_Y))[1]) > 1:
            cy += 32 if cy < to_px((0, CORRIDOR_Y))[1] else -32
        d.ellipse((cx - 28, cy - 28, cx + 28, cy + 28), fill=color, outline="#FFFFFF", width=8)

    dot(pts[0], START)
    dot(pts[-1], GOAL)
    top_band = lambda place: to_px((BOXES[place][0] + BOXES[place][2] / 2, BOXES[place][1] + 34))   # above the room's name
    sx, sy = pts[0]
    if leg["from"] in GATE_X:
        _pill(d, start_label, (sx, sy + 190), START, img.width)
    elif leg["from"] in BOXES:
        _pill(d, start_label, top_band(leg["from"]), START, img.width)
    else:
        _pill(d, start_label, to_px((LIFT_X, plans.CORR1 + 36)), START, img.width)
    if leg["to"] in BOXES:
        _pill(d, goal_label, top_band(leg["to"]), GOAL, img.width)
    else:
        _pill(d, goal_label, to_px((LIFT_X, plans.BY0 + 36)), GOAL, img.width)
    img = img.resize((OUT_WIDTH, round(img.height * OUT_WIDTH / img.width)), Image.LANCZOS)
    head_h = 76
    out = Image.new("RGB", (img.width, img.height + head_h), NAVY)
    out.paste(img, (0, head_h))
    size = 44
    while ImageDraw.Draw(out).textlength(title, font=_font(size)) > OUT_WIDTH - 40 and size > 20:
        size -= 2
    ImageDraw.Draw(out).text((24, head_h / 2), title, font=_font(size), fill="#FFFFFF", anchor="lm")
    return out


@functools.lru_cache(maxsize=256)
def draw(start, dest):
    """The walkthrough map as JPEG bytes (fixed for a start and destination, so it is cached)."""
    trip = legs(start, dest)
    if not trip:
        raise ValueError("already there")
    panels = []
    for i, leg in enumerate(trip):
        step = f"Step {i + 1} of {len(trip)}: " if len(trip) > 1 else ""
        floor = FLOOR_NAMES[leg["floor"]]
        is_lift_end = leg["to"].startswith("LIFT-")
        goal_label = f"Take lift to {FLOOR_NAMES[floor_of(dest)].lower()}" if is_lift_end else "GO HERE"
        start_label = "Leave the lift here" if leg["from"].startswith("LIFT-") and i > 0 else "YOU ARE HERE"
        target = "main lift" if is_lift_end else _name(dest)
        panels.append(_panel(leg, f"{step}{floor}: walk to {target}", start_label, goal_label))
    foot = 56
    canvas = Image.new("RGB", (OUT_WIDTH, sum(p.height for p in panels) + 8 * (len(panels) - 1) + foot), "#FFFFFF")
    y = 0
    for p in panels:
        canvas.paste(p, (0, y))
        y += p.height + 8
    ImageDraw.Draw(canvas).text((24, y + foot / 2 - 8), "Sample layout for the OncoSaathi demo. Not the real floor plan.",
                                font=_font(34), fill="#B3261E", anchor="lm")
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=86, optimize=True)
    return buf.getvalue()


def start_choices():
    """Places a patient can say they are standing at, by floor, for the chat's "I'm here" picker."""
    out = [{"id": "GATE1", "name": "Gate 1 (main entrance)", "floor": "G"}, {"id": "GATE2", "name": "Gate 2 (emergency)", "floor": "G"}]
    for fid in plans.ORDER:
        out.append({"id": f"LIFT-{fid}", "name": "Main lift", "floor": fid})
        out += [{"id": u["id"], "name": u["name"], "floor": fid} for u, *_ in plans.room_boxes(fid) if u.get("public") is not False]
    return out
