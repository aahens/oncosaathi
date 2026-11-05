"""Export the hospital map pictures from hospital.json (the TMH Patient Booklet layout) as PNG files, for the README and slides.

Run:  python make_floor_plans.py     -> floor_plans/campus.png and floor_plans/<building>_<floor>.png
route_map.py draws them, and the server serves the same drawings live at /map/campus.png and /map/floor.png?b=GJB&f=1,
so the pictures, the route maps and the spoken answers all come from one data file. Buildings, floors and counters are
from the booklet; building positions and room positions inside a floor are illustrative (the booklet has no map).
"""
import os

import route_map

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "floor_plans")

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    jobs = [("campus", route_map.campus_base())] + [(f"{b}_{f}", route_map.floor_base(b, f)) for b, f in route_map.used_floors()]
    for name, img in jobs:
        path = os.path.join(OUT, name + ".png")
        with open(path, "wb") as fh:
            fh.write(route_map.png(img))
        print("wrote", path)
