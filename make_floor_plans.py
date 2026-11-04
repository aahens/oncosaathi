"""Draw architectural-style floor plans (one per floor) from hospital.json, as SVG and PNG.

Run:  .venv/bin/python make_floor_plans.py        -> floor_plans/floor_G.svg/.png ... floor_4.svg/.png
The drawing is generated from the same data the agent speaks from, so map and voice can never disagree.
PNG rendering uses the installed Google Chrome in headless mode.
"""
import html
import json
import os
import pathlib
import shutil
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "floor_plans")
H = json.load(open(os.path.join(HERE, "hospital.json"), encoding="utf-8"))
CHROME_PATHS = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"]

W, HGT = 1700, 1180
BX0, BY0, BY1 = 150, 230, 790            # building band
CORR0, CORR1 = 480, 540                  # main corridor (y)
WINGS = [("A", 150, 560), ("CORE", 560, 680), ("B", 680, 960), ("C", 960, 1240), ("D", 1240, 1570)]
FILL = {"opd": "#D9D3FB", "diagnostic": "#C9EDE1", "treatment": "#FBD9CB", "emergency": "#F7C1C1",
        "support": "#DCE8F7", "ward": "#E9E7E1"}
INK, NAVY, ROAD, BLDG, EDGE = "#1F2A44", "#1E2B5C", "#E9EEF3", "#AFC6E4", "#5B7BA8"
ORDER = ["G", "1", "2", "3", "4"]
FLOOR_NAME = {f["id"]: f["name"] for f in H["floors"]}
# One "Excuse me…" example per floor, answered with the path text stored in hospital.json.
EXAMPLE = {"G": "B-G-SAMPLE", "1": "C-1-CT", "2": "B-2-ENDO", "3": "B-3-PAC", "4": "A-4-PSY"}


def esc(s):
    return html.escape(str(s), quote=True)


def wrap(text, width):
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


def icon(x, y, glyph, size=30, fs=17, bg=NAVY):
    return (f'<g><rect x="{x}" y="{y}" width="{size}" height="{size}" rx="5" fill="{bg}"/>'
            f'<text x="{x + size / 2}" y="{y + size / 2 + 1}" font-size="{fs}" fill="#fff" text-anchor="middle" '
            f'dominant-baseline="central" font-weight="700">{esc(glyph)}</text></g>')


def room(x, y, w, h, u):
    staff = u.get("public") is False
    fill = FILL.get(u["cat"], "#EEE")
    dash = ' stroke-dasharray="6 4"' if staff else ""
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" stroke="{EDGE}" stroke-width="1.5"{dash}/>']
    lines = wrap(u["name"], max(9, int(w / 9.2)))[:4]
    ty = y + h / 2 - (len(lines) - 1) * 9 - 12
    for i, ln in enumerate(lines):
        out.append(f'<text x="{x + w / 2}" y="{ty + i * 18}" font-size="15" font-weight="700" fill="{INK}" text-anchor="middle">{esc(ln)}</text>')
    parts = [p.strip() for p in u["rooms"].replace(";", ",").split(",")]
    tag = "Staff only" if staff else next((p for p in parts if any(c.isdigit() for c in p)), parts[0])
    tag = tag.replace("Rooms ", "").replace("Room ", "").replace("counters", "Counters").replace("counter", "Counter")
    fs = min(12.5, (w - 22) / (len(tag) * 0.62))
    tw = min(w - 12, 12 + len(tag) * fs * 0.62)
    out.append(f'<rect x="{x + w / 2 - tw / 2}" y="{y + h - 32}" width="{tw}" height="22" rx="4" fill="{NAVY if not staff else "#7A7A74"}"/>')
    out.append(f'<text x="{x + w / 2}" y="{y + h - 21}" font-size="{fs:.1f}" fill="#fff" text-anchor="middle" dominant-baseline="central" font-weight="700">{esc(tag)}</text>')
    return "".join(out)


def room_boxes(fid):
    """Where each room of a floor is drawn: [(unit, x, y, w, h)]. route_map.py uses the same boxes for the route line."""
    units = [u for u in H["units"] if u["floor"] == fid]
    boxes = []
    for wid, x0, x1 in WINGS:
        us = [u for u in units if u["block"] == wid]
        if wid == "CORE":
            boxes += [(u, x0 + 12, CORR1 + 20, x1 - x0 - 24, BY1 - CORR1 - 40) for u in us]
            continue
        top, bot = us[: (len(us) + 1) // 2], us[(len(us) + 1) // 2:]
        for row, (ry0, ry1) in ((top, (BY0 + 16, CORR0 - 14)), (bot, (CORR1 + 14, BY1 - 16))):
            if row:
                cw = (x1 - x0 - 16) / len(row)
                boxes += [(u, x0 + 8 + i * cw + 4, ry0, cw - 8, ry1 - ry0) for i, u in enumerate(row)]
    return boxes


def plan(fid):
    units = [u for u in H["units"] if u["floor"] == fid]
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{HGT}" viewBox="0 0 {W} {HGT}" '
         f'font-family="Arial, Helvetica, sans-serif">',
         f'<rect width="{W}" height="{HGT}" fill="#ffffff"/>']
    # title
    s.append(f'<text x="60" y="78" font-size="40" font-weight="700" fill="{NAVY}">Tata Memorial Hospital, Parel</text>')
    s.append(f'<text x="60" y="122" font-size="30" fill="{INK}">{esc(FLOOR_NAME[fid])}</text>')
    s.append(f'<text x="{W - 60}" y="78" font-size="44" font-weight="700" fill="{NAVY}" text-anchor="end">'
             f'{"GROUND" if fid == "G" else "LEVEL " + fid}</text>')
    s.append(f'<text x="{W - 60}" y="112" font-size="16" fill="#B3261E" text-anchor="end">Sample layout for the OncoSaathi demo. '
             f'Not the real floor plan.</text>')
    # roads (ground floor) / outline
    if fid == "G":
        s.append(f'<rect x="20" y="{BY1 + 20}" width="{W - 40}" height="44" fill="{ROAD}"/>')
        s.append(f'<text x="{W / 2}" y="{BY1 + 48}" font-size="17" fill="#5F6B7A" text-anchor="middle" letter-spacing="4">MAIN ROAD</text>')
    # building shell
    s.append(f'<rect x="{BX0}" y="{BY0}" width="{WINGS[-1][2] - BX0}" height="{BY1 - BY0}" rx="18" fill="{BLDG}" stroke="{EDGE}" stroke-width="3"/>')
    # wing separators + badges
    for wid, x0, x1 in WINGS:
        if wid != "A":
            s.append(f'<line x1="{x0}" y1="{BY0}" x2="{x0}" y2="{BY1}" stroke="{EDGE}" stroke-width="2"/>')
        cx = (x0 + x1) / 2
        label = "Core" if wid == "CORE" else f"Wing {wid}"
        s.append(f'<circle cx="{cx - 40 if wid != "CORE" else cx}" cy="{BY0 - 34}" r="20" fill="#1B7A4C"/>'
                 f'<text x="{cx - 40 if wid != "CORE" else cx}" y="{BY0 - 33}" font-size="20" font-weight="700" fill="#fff" '
                 f'text-anchor="middle" dominant-baseline="central">{"⇅" if wid == "CORE" else wid}</text>')
        if wid != "CORE":
            s.append(f'<text x="{cx - 12}" y="{BY0 - 26}" font-size="22" font-weight="700" fill="#1B7A4C">WING</text>')
    # corridor
    s.append(f'<rect x="{BX0 + 8}" y="{CORR0}" width="{WINGS[-1][2] - BX0 - 16}" height="{CORR1 - CORR0}" fill="#E9F1FB" stroke="{EDGE}" stroke-width="1"/>')
    s.append(f'<text x="{(WINGS[2][1] + WINGS[3][2]) / 2}" y="{(CORR0 + CORR1) / 2 + 1}" font-size="15" fill="#5F6B7A" '
             f'text-anchor="middle" dominant-baseline="central" letter-spacing="3">MAIN CORRIDOR</text>')
    # rooms per wing: alternate above / below the corridor
    for u, x, y, w, h in room_boxes(fid):
        s.append(room(x, y, w, h, u))
    for wid, x0, x1 in WINGS:
        us = [u for u in units if u["block"] == wid]
        if wid == "CORE":
            lx = (x0 + x1) / 2
            s.append(f'<rect x="{x0 + 18}" y="{CORR0 - 120}" width="{x1 - x0 - 36}" height="100" rx="8" fill="#ffffff" stroke="{EDGE}" stroke-width="1.5"/>')
            s.append(icon(lx - 15, CORR0 - 108, "⇅"))
            s.append(f'<text x="{lx}" y="{CORR0 - 52}" font-size="14" font-weight="700" fill="{INK}" text-anchor="middle">Main lift</text>')
            s.append(f'<text x="{lx}" y="{CORR0 - 34}" font-size="12" fill="#5F6B7A" text-anchor="middle">4 cars, G–4</text>')
            continue
        if not us:
            s.append(f'<text x="{(x0 + x1) / 2}" y="{(BY0 + CORR0) / 2}" font-size="15" fill="#5F6B7A" text-anchor="middle">'
                     f'No public departments on this floor</text>')
    # stairs at both ends, washrooms at both ends of the corridor, Wing D stretcher lift
    for x in (BX0 + 14, WINGS[-1][2] - 44):
        s.append(icon(x, CORR0 + 15, "≡"))
    s.append(icon(BX0 + 50, CORR0 + 15, "WC", fs=12))
    s.append(icon(WINGS[-1][2] - 80, CORR0 + 15, "WC", fs=12))
    s.append(icon(WINGS[-1][1] + 24, CORR0 + 15, "⇅", bg="#4A5A7A"))
    # ground floor: gates and help desk. route_map.py marks where the patient really is, so the plan has no fixed "you are here".
    if fid == "G":
        gy = (CORR0 + CORR1) / 2
        s.append(f'<polygon points="{BX0 - 90},{gy - 20} {BX0 - 50},{gy} {BX0 - 90},{gy + 20}" fill="{NAVY}"/>')
        s.append(f'<text x="{BX0 - 70}" y="{gy - 34}" font-size="15" font-weight="700" fill="{NAVY}" text-anchor="middle">Gate 1</text>')
        s.append(f'<text x="{BX0 - 70}" y="{gy + 44}" font-size="12" fill="{INK}" text-anchor="middle">Main entrance</text>')
        ex = WINGS[-1][2]
        s.append(f'<polygon points="{ex + 46},{gy - 20} {ex + 6},{gy} {ex + 46},{gy + 20}" fill="#C62828"/>')
        s.append(f'<text x="{ex + 30}" y="{gy - 34}" font-size="15" font-weight="700" fill="#C62828" text-anchor="middle">Gate 2</text>')
        s.append(f'<text x="{ex + 30}" y="{gy + 44}" font-size="12" fill="#C62828" text-anchor="middle">Emergency</text>'
                 f'<text x="{ex + 30}" y="{gy + 60}" font-size="12" fill="#C62828" text-anchor="middle">24×7</text>')
        s.append(icon(BX0 + 86, CORR0 + 15, "i", fs=19, bg="#1B7A4C"))
    # key
    ky = 920
    s.append(f'<text x="60" y="{ky}" font-size="22" font-weight="700" fill="{NAVY}">KEY</text>')
    keys = [("⇅", "Lift", NAVY, 17), ("≡", "Stairs", NAVY, 17), ("WC", "Washrooms & water", NAVY, 12), ("i", "Help desk", "#1B7A4C", 19)]
    for i, (g, lab, bg, fs) in enumerate(keys):
        x, y = 60 + (i % 2) * 230, ky + 20 + (i // 2) * 44
        s.append(icon(x, y, g, fs=fs, bg=bg))
        s.append(f'<text x="{x + 42}" y="{y + 20}" font-size="16" fill="{INK}">{esc(lab)}</text>')
    cats = [("opd", "OPD & clinics"), ("diagnostic", "Tests & scans"), ("treatment", "Treatment"),
            ("support", "Patient services"), ("emergency", "Emergency"), ("ward", "Wards / staff only (dashed)")]
    for i, (c, lab) in enumerate(cats):
        x, y = 60 + (i % 2) * 230, ky + 112 + (i // 2) * 36
        s.append(f'<rect x="{x}" y="{y}" width="30" height="22" rx="4" fill="{FILL[c]}" stroke="{EDGE}"/>')
        s.append(f'<text x="{x + 42}" y="{y + 16}" font-size="16" fill="{INK}">{esc(lab)}</text>')
    # "Excuse me" box
    u = next(x for x in H["units"] if x["id"] == EXAMPLE[fid])
    bx = 560
    s.append(f'<rect x="{bx}" y="{ky - 30}" width="{W - bx - 60}" height="230" rx="12" fill="#F6F8FB" stroke="#D5DEEA"/>')
    s.append(f'<text x="{bx + 24}" y="{ky + 6}" font-size="20" font-weight="700" fill="{NAVY}">Patient: Excuse me, where is {esc(u["name"])}?</text>')
    for i, ln in enumerate(wrap("OncoSaathi: " + u["path"], 92)[:4]):
        s.append(f'<text x="{bx + 24}" y="{ky + 46 + i * 28}" font-size="18" fill="{INK}">{esc(ln)}</text>')
    s.append(f'<text x="{bx + 24}" y="{ky + 180}" font-size="14" fill="#5F6B7A">Directions come from hospital.json, the same data '
             f'the OncoSaathi voice agent uses.</text>')
    s.append("</svg>")
    return "\n".join(s)


STAFF_DASH = ' stroke-dasharray="4 3" stroke="#999"'


def overview():
    """All five floors stacked, like a building directory board."""
    rows = []
    y = 150
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{HGT}" viewBox="0 0 {W} {HGT}" font-family="Arial, Helvetica, sans-serif">',
           f'<rect width="{W}" height="{HGT}" fill="#fff"/>',
           f'<text x="60" y="78" font-size="40" font-weight="700" fill="{NAVY}">Tata Memorial Hospital, Parel: building directory</text>',
           f'<text x="60" y="116" font-size="16" fill="#B3261E">Sample layout for the OncoSaathi demo. Not the real floor plan.</text>']
    cols = [("A", "Wing A · OPD & services", 150, 520), ("CORE", "Core", 520, 640), ("B", "Wing B · Labs", 640, 930),
            ("C", "Wing C · Imaging & wards", 930, 1220), ("D", "Wing D · Radiation, ER, ICU", 1220, 1640)]
    for _, lab, x0, x1 in cols:
        out.append(f'<text x="{(x0 + x1) / 2}" y="{y}" font-size="16" font-weight="700" fill="{NAVY}" text-anchor="middle">{esc(lab)}</text>')
    rh = 178
    for i, fid in enumerate(reversed(ORDER)):
        ry = y + 16 + i * rh
        out.append(f'<rect x="60" y="{ry}" width="80" height="{rh - 10}" rx="8" fill="{NAVY}"/>'
                   f'<text x="100" y="{ry + (rh - 10) / 2}" font-size="30" font-weight="700" fill="#fff" text-anchor="middle" dominant-baseline="central">{fid}</text>')
        for wid, _, x0, x1 in cols:
            out.append(f'<rect x="{x0 + 4}" y="{ry}" width="{x1 - x0 - 8}" height="{rh - 10}" rx="8" fill="#F3F6FA" stroke="#D5DEEA"/>')
            us = [u for u in H["units"] if u["floor"] == fid and u["block"] == wid]
            if wid == "CORE":
                out.append(f'<text x="{(x0 + x1) / 2}" y="{ry + 30}" font-size="14" fill="{INK}" text-anchor="middle">⇅ Main lift</text>')
            for k, u in enumerate(us[:6]):
                room_chars = int((x1 - x0 - 40) / 7.2)
                name = "Pharmacy" if u["id"] == "CORE-G-PHARM" else u["name"]
                txt = name if len(name) <= room_chars else name[:room_chars - 1] + "…"
                out.append(f'<rect x="{x0 + 12}" y="{ry + 10 + k * 26 + (24 if wid == "CORE" else 0)}" width="{x1 - x0 - 24}" height="22" rx="4" '
                           f'fill="{FILL.get(u["cat"])}"{STAFF_DASH if u.get("public") is False else ""}/>')
                out.append(f'<text x="{x0 + 20}" y="{ry + 25 + k * 26 + (24 if wid == "CORE" else 0)}" font-size="13" fill="{INK}">{esc(txt)}</text>')
    out.append("</svg>")
    return "\n".join(out)


def render(svg_path, png_path):
    chrome = next((p for p in CHROME_PATHS if os.path.exists(p)), None) or shutil.which("google-chrome")
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--window-size={W},{HGT}", f"--screenshot={png_path}", "--default-background-color=FFFFFFFF",
                    pathlib.Path(svg_path).as_uri()], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    jobs = [(f"floor_{fid}", plan(fid)) for fid in ORDER] + [("building_directory", overview())]
    for name, svg in jobs:
        sp, pp = os.path.join(OUT, name + ".svg"), os.path.join(OUT, name + ".png")
        open(sp, "w", encoding="utf-8").write(svg)
        render(sp, pp)
        print("wrote", pp)
