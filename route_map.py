"""Walkthrough map for the Tata Memorial booklet layout: the route from where the patient is to the department, as a JPEG.

Places:
    GATE1, GATE2                 the main gate (Dr Ernest Borges Road) and the GJB back gate
    <unit id>                    a department at its general location, e.g. "C-1-MRI"
    <unit id>@private            its location for private patients
    <unit id>@2, @3              its other listed locations (CT is in the MB and GJB basements)
    LIFT-<building>-<floor>      the lift lobby on a building floor
Which building and floor a department is on, and its counter numbers, come from hospital.json (the TMH Patient Booklet).
Where the buildings stand and where a room sits inside its floor are drawn by us: the booklet has no map, so every
image says the positions are illustrative. A trip has up to three panels: the start floor out to the lift, the campus
from building to building, and the destination floor from the lift (or the entrance) to the room. The image holds
nothing about the patient, so the same URL works for web chat and WhatsApp.
"""
import functools
import io
import json
import math
import os
import re

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
H = json.load(open(os.path.join(HERE, "hospital.json"), encoding="utf-8"))
UNITS = {u["id"]: u for u in H["units"]}
BUILDINGS = {b["id"]: b for b in H["blocks"]}
FLOOR_NAMES = {f["id"]: f["name"] for f in H["floors"]}
GATES = {"GATE1": "Main gate", "GATE2": "GJB back gate"}
GATE_X = GATES                                            # older name, still used by brain.walk_dest
MAP_VERSION = 3                                           # raise when the drawing changes, so browsers fetch the new image
FONTS = ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf")
FONTS_REG = ("arial.ttf", "Arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf")
ROUTE, CASING, START, GOAL, NAVY, INK, MUTED, RED = "#0B57D0", "#FFFFFF", "#C62828", "#1B7A4C", "#1E2B5C", "#1F2A44", "#5F6B7A", "#B3261E"
FILL = {"opd": "#D9D3FB", "diagnostic": "#C9EDE1", "treatment": "#FBD9CB", "emergency": "#F7C1C1", "support": "#DCE8F7", "ward": "#E9E7E1"}
NOTE = "Buildings, floors and counters: TMH Patient Booklet 2025. Positions on this map are illustrative, not the real plan."
OUT_WIDTH = 1600

# ---------------- where things are ----------------
SPOTS = {}                                                # spot id -> {"unit", "block", "floor", "rooms", "kind"}
for _u in H["units"]:
    if "block" not in _u or _u.get("public") is False:
        continue
    SPOTS[_u["id"]] = {"unit": _u, "block": _u["block"], "floor": _u["floor"], "rooms": _u.get("rooms", ""), "kind": "general"}
    if "private" in _u:
        p = _u["private"]
        SPOTS[_u["id"] + "@private"] = {"unit": _u, "block": p["block"], "floor": p["floor"], "rooms": p.get("rooms", ""), "kind": "private"}
    for _i, _a in enumerate(_u.get("also", []), 2):
        SPOTS[f"{_u['id']}@{_i}"] = {"unit": _u, "block": _a["block"], "floor": _a["floor"], "rooms": _a.get("rooms", ""), "kind": "also"}


def is_place(place):
    if not place:
        return False
    if place in GATES or place in SPOTS:
        return True
    if place.startswith("LIFT-"):
        parts = place.split("-")
        return len(parts) == 3 and parts[1] in BUILDINGS and parts[2] in BUILDINGS[parts[1]]["floors"]
    return False


def building_of(place):
    if place in GATES:
        return None
    if place.startswith("LIFT-"):
        return place.split("-")[1]
    return SPOTS[place]["block"]


def floor_of(place):
    if place in GATES:
        return "G"
    if place.startswith("LIFT-"):
        return place.split("-")[2]
    return SPOTS[place]["floor"]


def spot_for(uid, category=None):
    """The spot to send someone to: the private location for private patients, else the general one."""
    if category == "private" and f"{uid}@private" in SPOTS:
        return f"{uid}@private"
    return uid if uid in SPOTS else None


def floor_spots(block, floor):
    return [sid for sid, s in SPOTS.items() if s["block"] == block and s["floor"] == floor]


def label(place):
    if place in GATES:
        return GATES[place]
    if place.startswith("LIFT-"):
        return f"{place.split('-')[1]} lift"
    return SPOTS[place]["unit"]["name"]


# ---------------- campus geometry (illustrative) ----------------
CW, CH = 1600, 1000
SPINE_Y = 560
CAMPUS = {  # building: (x0, y0, x1, y1), door, where the door path meets the spine
    "AB": ((110, 230, 560, 490), (335, 490), (335, SPINE_Y)),
    "MB": ((640, 230, 960, 490), (800, 490), (800, SPINE_Y)),
    "SB": ((1040, 230, 1490, 490), (1265, 490), (1265, SPINE_Y)),
    "GJB": ((110, 630, 560, 860), (335, 630), (335, SPINE_Y)),
    "HBB": ((1040, 630, 1490, 860), (1265, 630), (1265, SPINE_Y)),
}
GATE_PT = {"GATE1": ((800, 950), (800, SPINE_Y)), "GATE2": ((40, SPINE_Y), (40, SPINE_Y))}


def campus_points(a, b):
    """Walking line on the campus from a gate or a building door (a) to building b's door: out to the spine, along it, in."""
    if a in GATE_PT:
        start, onto = GATE_PT[a]
    else:
        _, start, onto = CAMPUS[a]
    _, door, off = CAMPUS[b]
    pts = [start, onto, off, door]
    return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]


# ---------------- floor geometry (illustrative) ----------------
FW, FH = 1600, 760
CORR_Y0, CORR_Y1 = 370, 430
CORR_Y = (CORR_Y0 + CORR_Y1) // 2
LOBBY = (700, 450, 900, 600)
LIFT_DOOR = (800, LOBBY[1])
ENTRANCE = (800, 690)
TOP_ROW, BOTTOM_ROW = (130, 350), (450, 670)


@functools.lru_cache(maxsize=None)
def room_boxes(block, floor):
    """{spot id: (x, y, w, h)} for one building floor: rooms above the corridor first, then below it on either side of the lobby."""
    ids = floor_spots(block, floor)
    n = len(ids)
    top_n = n if n <= 6 else min(8, (n + 1) // 2)
    top, rest = ids[:top_n], ids[top_n:]
    left, right = rest[:(len(rest) + 1) // 2], rest[(len(rest) + 1) // 2:]
    boxes = {}

    def row(items, x0, x1, y0, y1):
        if not items:
            return
        w = (x1 - x0) / len(items)
        for i, sid in enumerate(items):
            boxes[sid] = (x0 + i * w + 8, y0, w - 16, y1 - y0)
    row(top, 80, 1520, *TOP_ROW)
    row(left, 80, LOBBY[0] - 20, *BOTTOM_ROW)
    row(right, LOBBY[2] + 20, 1520, *BOTTOM_ROW)
    return boxes


def _door(block, floor, place):
    """(door point, where it meets the corridor) for a room, the lift or the entrance on one floor."""
    if place == "ENTRANCE":
        return ENTRANCE, (ENTRANCE[0], CORR_Y)
    if place.startswith("LIFT-"):
        return LIFT_DOOR, (LIFT_DOOR[0], CORR_Y)
    x, y, w, h = room_boxes(block, floor)[place]
    cx = x + w / 2
    return ((cx, y + h) if y < CORR_Y else (cx, y)), (cx, CORR_Y)


def floor_points(block, floor, a, b):
    if {a, b} == {f"LIFT-{block}-{floor}", "ENTRANCE"}:  # straight through the lobby
        pts = [(LIFT_DOOR[0], LOBBY[3] - 30), ENTRANCE]
        return pts if a.startswith("LIFT-") else pts[::-1]
    (pa, ca), (pb, cb) = _door(block, floor, a), _door(block, floor, b)
    pts = [pa, ca, cb, pb]
    return [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]


def _inside(block, floor, place):
    """Where a label goes for a place on a floor: inside the room on the side away from its door, or by the lift / entrance."""
    if place == "ENTRANCE":
        return (ENTRANCE[0], 640)
    if place.startswith("LIFT-"):
        return (LIFT_DOOR[0], 628)
    x, y, w, h = room_boxes(block, floor)[place]
    return (x + w / 2, y + 32) if y < CORR_Y else (x + w / 2, y + h - 32)


def _campus_label_at(bid):
    """Inside a building, next to its door: where its "go in" label goes."""
    (x0, y0, x1, y1), door, _ = CAMPUS[bid]
    return ((x0 + x1) / 2, y1 - 36) if door[1] == y1 else ((x0 + x1) / 2, y0 + 36)


def trip(start, dest):
    """The panels of the walk: [{"kind": "campus"|"floor", "block", "floor", "from", "to"}]. Empty if already there."""
    if start == dest:
        return []
    db, df = building_of(dest), floor_of(dest)
    arrive = "ENTRANCE" if df == "G" else f"LIFT-{db}-{df}"
    if start in GATES:
        return [{"kind": "campus", "block": None, "floor": "G", "from": start, "to": db},
                {"kind": "floor", "block": db, "floor": df, "from": arrive, "to": dest}]
    sb, sf = building_of(start), floor_of(start)
    if sb == db:
        if sf == df:
            return [{"kind": "floor", "block": db, "floor": df, "from": start, "to": dest}]
        out = []
        if not start.startswith("LIFT-"):
            out.append({"kind": "floor", "block": sb, "floor": sf, "from": start, "to": f"LIFT-{sb}-{sf}"})
        return out + [{"kind": "floor", "block": db, "floor": df, "from": f"LIFT-{db}-{df}", "to": dest}]
    out = []
    if sf == "G":                                         # walk out of the entrance (from a room or the lift lobby)
        out.append({"kind": "floor", "block": sb, "floor": "G", "from": start, "to": "ENTRANCE"})
    elif not start.startswith("LIFT-"):                   # upstairs: to the lift first (then down and out)
        out.append({"kind": "floor", "block": sb, "floor": sf, "from": start, "to": f"LIFT-{sb}-{sf}"})
    return out + [{"kind": "campus", "block": None, "floor": "G", "from": sb, "to": db},
                  {"kind": "floor", "block": db, "floor": df, "from": arrive, "to": dest}]


# ---------------- drawing ----------------
@functools.lru_cache(maxsize=None)
def _font(size, bold=True):
    for name in (FONTS if bold else FONTS_REG):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def _wrap(d, text, font, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if cur and d.textlength(test, font=font) > width:
            lines.append(cur)
            cur = w
        else:
            cur = test
    return lines + ([cur] if cur else [])


def _lift_icon(d, x, y, size=40):
    """A lift: two arrows, up and down (drawn, so it doesn't depend on the font having ⇅)."""
    d.rounded_rectangle((x, y, x + size, y + size), radius=7, fill=NAVY)
    cx, s = x + size / 2, size / 5
    d.polygon([(cx - s, y + size * 0.45), (cx + s, y + size * 0.45), (cx, y + size * 0.15)], fill="#FFFFFF")
    d.polygon([(cx - s, y + size * 0.55), (cx + s, y + size * 0.55), (cx, y + size * 0.85)], fill="#FFFFFF")


def _stairs_icon(d, x, y, size=40):
    d.rounded_rectangle((x, y, x + size, y + size), radius=7, fill=NAVY)
    st, pts = size / 6, []
    for i in range(4):
        pts += [(x + st + i * st, y + size - st - i * st), (x + st + (i + 1) * st, y + size - st - i * st)]
    d.line(pts, fill="#FFFFFF", width=3)


def _tag(rooms):
    """Short counter label for a room box: 'Rotunda, counters 129 to 131' -> 'Rotunda, 129–131'."""
    t = re.sub(r"\b(counters?|rooms?|room no\.?)\s*", "", rooms, flags=re.I).replace(" to ", "–").replace(" and ", ", ")
    return re.sub(r"\s+", " ", t).strip(" ,")


@functools.lru_cache(maxsize=None)
def campus_base():
    img = Image.new("RGB", (CW, CH), "#FFFFFF")
    d = ImageDraw.Draw(img)
    d.rectangle((0, 900, CW, 1000), fill="#E9EEF3")
    d.text((CW / 2 + 260, 975), "DR ERNEST BORGES ROAD", font=_font(26), fill=MUTED, anchor="mm")
    for pts in ([(800, 950), (800, SPINE_Y)], [(40, SPINE_Y), (1265, SPINE_Y)]):
        d.line(pts, fill="#D7DEE7", width=46)
    for _, door, off in CAMPUS.values():
        d.line([door, off], fill="#D7DEE7", width=40)
    for bid, ((x0, y0, x1, y1), door, _) in CAMPUS.items():
        b = BUILDINGS[bid]
        d.rounded_rectangle((x0, y0, x1, y1), radius=18, fill="#AFC6E4", outline="#5B7BA8", width=4)
        top = y0 + 78 if door[1] == y1 else y1 - 132       # the name sits away from the door, the label space is by the door
        cx = (x0 + x1) / 2
        d.text((cx, top), bid, font=_font(64), fill=NAVY, anchor="mm")
        d.text((cx, top + 54), b["name"].replace(f" ({bid})", ""), font=_font(26), fill=INK, anchor="mm")
        fl = b["floors"]
        d.text((cx, top + 88), f"Floors {fl[0]}–{fl[-1]}", font=_font(22, False), fill=INK, anchor="mm")
        d.rectangle((door[0] - 34, door[1] - 6, door[0] + 34, door[1] + 6), fill=NAVY)
    d.polygon([(770, 950), (830, 950), (800, 905)], fill=NAVY)
    d.text((750, 935), "Main gate", font=_font(28), fill=NAVY, anchor="rm")
    d.polygon([(10, SPINE_Y - 28), (10, SPINE_Y + 28), (50, SPINE_Y)], fill=NAVY)
    d.text((14, SPINE_Y - 44), "GJB back gate", font=_font(22), fill=NAVY, anchor="lm")
    d.text((CW - 30, 40), "Tata Memorial Hospital, Parel: campus", font=_font(34), fill=NAVY, anchor="rm")
    d.text((CW - 30, 82), "Building positions are illustrative, not the real campus map", font=_font(22, False), fill=RED, anchor="rm")
    return img


@functools.lru_cache(maxsize=None)
def floor_base(block, floor):
    img = Image.new("RGB", (FW, FH), "#FFFFFF")
    d = ImageDraw.Draw(img)
    b = BUILDINGS[block]
    d.text((60, 52), f"{b['name']}: {FLOOR_NAMES[floor].lower()}", font=_font(34), fill=NAVY, anchor="lm")
    d.text((FW - 60, 52), "Room positions are illustrative", font=_font(22, False), fill=RED, anchor="rm")
    d.rounded_rectangle((60, 110, 1540, 690), radius=18, fill="#EEF3FA", outline="#5B7BA8", width=4)
    d.rectangle((80, CORR_Y0, 1520, CORR_Y1), fill="#FFFFFF", outline="#C9D6E6")
    d.text((1200, CORR_Y), "CORRIDOR", font=_font(18), fill=MUTED, anchor="mm")
    d.rounded_rectangle(LOBBY, radius=10, fill="#FFFFFF", outline="#5B7BA8", width=2)
    _lift_icon(d, LOBBY[0] + 22, LOBBY[1] + 26)
    _stairs_icon(d, LOBBY[2] - 62, LOBBY[1] + 26)
    d.text((LOBBY[0] + 42, LOBBY[1] + 92), "Lift", font=_font(20), fill=INK, anchor="mm")
    d.text((LOBBY[2] - 42, LOBBY[1] + 92), "Stairs", font=_font(20), fill=INK, anchor="mm")
    if floor == "G":
        d.rectangle((ENTRANCE[0] - 60, 682, ENTRANCE[0] + 60, 698), fill=NAVY)
        d.text((ENTRANCE[0], 722), "Entrance", font=_font(22), fill=NAVY, anchor="mm")
    boxes = room_boxes(block, floor)
    for sid, (x, y, w, h) in boxes.items():
        s = SPOTS[sid]
        u = s["unit"]
        d.rounded_rectangle((x, y, x + w, y + h), radius=8, fill=FILL.get(u["cat"], "#EEEEEE"), outline="#5B7BA8", width=2)
        size = 21 if w > 200 else 18 if w > 150 else 16
        lines = _wrap(d, u["name"], _font(size), w - 16)[:5]
        ty = y + h / 2 - (len(lines) - 1) * (size + 4) / 2 - 16
        for i, ln in enumerate(lines):
            d.text((x + w / 2, ty + i * (size + 4)), ln, font=_font(size), fill=INK, anchor="mm")
        tag = _tag(s["rooms"])
        if s["kind"] == "private":
            tag = f"Private · {tag}" if tag else "Private"
        if tag:
            f = _font(15)
            while d.textlength(tag, font=f) > w - 18 and f.size > 10:
                f = _font(f.size - 1)
            tw = min(w - 10, d.textlength(tag, font=f) + 16)
            d.rounded_rectangle((x + w / 2 - tw / 2, y + h - 34, x + w / 2 + tw / 2, y + h - 10), radius=5, fill=NAVY)
            d.text((x + w / 2, y + h - 22), tag, font=f, fill="#FFFFFF", anchor="mm")
    if not boxes:
        d.text((FW / 2, 240), "No departments listed on this floor", font=_font(24, False), fill=MUTED, anchor="mm")
    return img


def _arrow(d, at, angle, size=26):
    pts = [(at[0] + size * math.cos(angle + a), at[1] + size * math.sin(angle + a)) for a in (0, 2.3, -2.3)]
    d.polygon(pts, fill=NAVY, outline="#FFFFFF", width=3)


def _route(d, pts):
    d.line(pts, fill=CASING, width=24, joint="curve")
    d.line(pts, fill=ROUTE, width=14, joint="curve")
    for a, b in zip(pts, pts[1:]):
        length, angle = math.dist(a, b), math.atan2(b[1] - a[1], b[0] - a[0])
        if length < 90:
            continue
        for i in range(max(1, int(length // 260))):
            t = (i + 0.5) / max(1, int(length // 260))
            _arrow(d, (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t), angle)


def _dot(d, p, color):
    d.ellipse((p[0] - 16, p[1] - 16, p[0] + 16, p[1] + 16), fill=color, outline="#FFFFFF", width=5)


def _pill(d, text, center, color, width_limit):
    f = _font(26)
    w = d.textlength(text, font=f) + 30
    x = min(max(center[0] - w / 2, 8), width_limit - w - 8)
    d.rounded_rectangle((x, center[1] - 22, x + w, center[1] + 22), radius=12, fill=color, outline="#FFFFFF", width=4)
    d.text((x + w / 2, center[1]), text, font=f, fill="#FFFFFF", anchor="mm")


def _header(img, title):
    head = 64
    out = Image.new("RGB", (img.width, img.height + head), NAVY)
    out.paste(img, (0, head))
    d = ImageDraw.Draw(out)
    size = 34
    while d.textlength(title, font=_font(size)) > img.width - 40 and size > 18:
        size -= 2
    d.text((22, head / 2), title, font=_font(size), fill="#FFFFFF", anchor="lm")
    return out


def _campus_panel(leg, title, dest):
    img = campus_base().copy()
    d = ImageDraw.Draw(img)
    (x0, y0, x1, y1), _, _ = CAMPUS[leg["to"]]
    d.rounded_rectangle((x0 - 6, y0 - 6, x1 + 6, y1 + 6), radius=22, outline=GOAL, width=10)
    pts = campus_points(leg["from"], leg["to"])
    _route(d, pts)
    _dot(d, pts[0], START)
    _dot(d, pts[-1], GOAL)
    if leg["from"] in GATES:
        _pill(d, "YOU ARE HERE", (pts[0][0] + (150 if leg["from"] == "GATE1" else 120), pts[0][1] - 40), START, CW)
    else:
        _pill(d, f"Leave {leg['from']} here", _campus_label_at(leg["from"]), START, CW)
    df = floor_of(dest)
    goal = "Go in here" if df == "G" else ("Go in, lift down to basement" if df == "B" else f"Go in, lift to floor {df}")
    _pill(d, goal, _campus_label_at(leg["to"]), GOAL, CW)
    return _header(img, title)


def _floor_panel(leg, title, start_label, goal_label):
    b, f = leg["block"], leg["floor"]
    img = floor_base(b, f).copy()
    d = ImageDraw.Draw(img)
    boxes = room_boxes(b, f)
    if leg["to"] in boxes:
        x, y, w, h = boxes[leg["to"]]
        d.rounded_rectangle((x - 5, y - 5, x + w + 5, y + h + 5), radius=12, outline=GOAL, width=9)
    pts = floor_points(b, f, leg["from"], leg["to"])
    _route(d, pts)
    _dot(d, pts[0], START)
    _dot(d, pts[-1], GOAL)
    _pill(d, start_label, _inside(b, f, leg["from"]), START, FW)
    _pill(d, goal_label, _inside(b, f, leg["to"]), GOAL, FW)
    return _header(img, title)


@functools.lru_cache(maxsize=256)
def draw(start, dest):
    """The walkthrough map as JPEG bytes (fixed for a start and destination, so it is cached)."""
    legs = trip(start, dest)
    if not legs:
        raise ValueError("already there")
    panels = []
    for i, leg in enumerate(legs):
        step = f"Step {i + 1} of {len(legs)}: " if len(legs) > 1 else ""
        if leg["kind"] == "campus":
            name = BUILDINGS[leg["to"]]["name"]
            panels.append(_campus_panel(leg, f"{step}walk to the {name}", dest))
            continue
        b, f = leg["block"], leg["floor"]
        where = f"{b}, {FLOOR_NAMES[f].lower()}"
        if leg["to"] == "ENTRANCE":
            panels.append(_floor_panel(leg, f"{step}{where}: walk out of the {b} entrance", "YOU ARE HERE", "Exit here"))
        elif leg["to"].startswith("LIFT-"):
            panels.append(_floor_panel(leg, f"{step}{where}: walk to the lift", "YOU ARE HERE", "Take the lift"))
        else:
            start_label = ("Enter here" if leg["from"] == "ENTRANCE" else "Leave the lift here") if i > 0 or leg["from"] == "ENTRANCE" \
                else ("YOU ARE HERE" if not leg["from"].startswith("LIFT-") else "Leave the lift here")
            panels.append(_floor_panel(leg, f"{step}{where}: walk to {label(dest)}", start_label, "GO HERE"))
    foot = 52
    canvas = Image.new("RGB", (OUT_WIDTH, sum(p.height for p in panels) + 8 * (len(panels) - 1) + foot), "#FFFFFF")
    y = 0
    for p in panels:
        canvas.paste(p, (0, y))
        y += p.height + 8
    ImageDraw.Draw(canvas).text((22, y + foot / 2 - 4), NOTE, font=_font(24, False), fill=RED, anchor="lm")
    buf = io.BytesIO()
    canvas.save(buf, "JPEG", quality=84, optimize=True)
    return buf.getvalue()


def png(img):
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def start_choices():
    """Places a patient can say they are standing at, for the chat's "I'm here" picker: the gates, then every department."""
    out = [{"id": gid, "name": name, "floor": "G", "block": None} for gid, name in GATES.items()]
    for sid, s in SPOTS.items():
        extra = " (private)" if s["kind"] == "private" else f" ({s['block']})" if s["kind"] == "also" else ""
        out.append({"id": sid, "name": s["unit"]["name"] + extra, "floor": s["floor"], "block": s["block"]})
    return out


def used_floors():
    """[(building, floor)] that have at least one department, in building order then floor order."""
    order = {f["id"]: i for i, f in enumerate(H["floors"])}
    pairs = {(s["block"], s["floor"]) for s in SPOTS.values()}
    return sorted(pairs, key=lambda p: (list(BUILDINGS).index(p[0]), order[p[1]]))
