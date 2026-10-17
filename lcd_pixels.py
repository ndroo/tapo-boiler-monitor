"""
lcd_pixels.py - read the P-950EH boiler LCD from pixels, no model.

Pipeline (all numpy + Pillow):
  1. The caller locates the LCD (monitor.locate_lcd -> crop box + which template matched).
  2. The crop is perspective-warped to a flat 640x220 grayscale rectangle using the glass
     corners calibrated for that template (docs/lcd_calib.json).
  3. The flat image is registered (+-6 px) against docs/lcd_flat_ref.png so a small camera
     nudge does not move the icon boxes.
  4. Darkness = (all-off reference docs/lcd_flat_off.png) - (frame): dirt, ghost segments and
     lighting gradients cancel. Each icon is "lit" when its box's mean darkness exceeds its
     calibrated threshold; the three temperature digits are decoded as slanted 7-segment cells.

Calibration (2026-10-17, 60 frames): every icon separable with a wide margin; temperature
exact on 59/60. Icons that never lit in that period (sun, snowflake, DHW box, [i], wrench,
DHW pump) have PROVISIONAL boxes placed from the manual's legend drawing - treat a first
"lit" on one of those as something to verify by eye.
"""
import json, os
import numpy as np
from PIL import Image, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
CALIB = os.path.join(HERE, "docs", "lcd_calib.json")
DIG = {(1,1,1,1,1,1,0):0,(0,1,1,0,0,0,0):1,(1,1,0,1,1,0,1):2,(1,1,1,1,0,0,1):3,(0,1,1,0,0,1,1):4,(1,0,1,1,0,1,1):5,
       (1,0,1,1,1,1,1):6,(1,1,1,0,0,0,0):7,(1,1,1,0,0,1,0):7,(1,1,1,1,1,1,1):8,(1,1,1,1,0,1,1):9}

_cache = {}
def _calib():
    if "c" not in _cache:
        c = json.load(open(CALIB))
        c["_ref"] = np.asarray(Image.open(os.path.join(HERE, "docs", c["ref"])).convert("L"), dtype=np.float32)
        c["_off"] = np.asarray(Image.open(os.path.join(HERE, "docs", c["off"])).convert("L"), dtype=np.float32)
        m = np.ones(c["_ref"].shape, bool)
        for x0, y0, x1, y1 in c["mask_exclude"]:
            m[y0:y1, x0:x1] = False
        c["_mask"] = m
        _cache["c"] = c
    return _cache["c"]

def _persp(src, dst):
    A, b = [], []
    for (x, y), (u, v) in zip(dst, src):
        A.append([x, y, 1, 0, 0, 0, -u * x, -u * y]); b.append(u)
        A.append([0, 0, 0, x, y, 1, -v * x, -v * y]); b.append(v)
    return np.linalg.solve(np.array(A, float), np.array(b, float))

def flatten(path, crop, template):
    """Flat 640x220 grayscale LCD for a frame, given its located crop and the matched template name."""
    c = _calib(); W, H = c["out_size"]; p = c["pad"]
    corners = c["corners"].get(os.path.basename(template) if template else "", None)
    if corners is None:
        raise KeyError(f"no LCD corners calibrated for template {template!r}")
    g = Image.open(path).convert("L").crop(tuple(crop))
    dst = [(p, p), (W - p, p), (W - p, H - p), (p, H - p)]
    return g.transform((W, H), Image.PERSPECTIVE, tuple(_persp([tuple(q) for q in corners], dst)), Image.BICUBIC)

def register(a, r=6):
    """(ncc, dx, dy): shift that best aligns frame `a` to the reference on the static regions."""
    c = _calib(); ref, m = c["_ref"], c["_mask"]; y = ref[m] - ref[m].mean(); yy = float((y * y).sum())
    best = None
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            b = np.roll(np.roll(a, -dy, 0), -dx, 1); x = b[m] - b[m].mean()
            s = float((x * y).sum() / np.sqrt((x * x).sum() * yy + 1e-6))
            if best is None or s > best[0]:
                best = (s, dx, dy)
    return best

def _seg_boxes(x, y, w, h, t, sk):
    m = y + h // 2
    X = lambda yy: x + sk * (y + h - yy) / h
    segs = [(X(y)+t, y, X(y)+w-t, y+t), (X(y+t)+w-t, y+t, X(m)+w, m-t//2), (X(m)+w-t, m+t//2, X(y+h)+w, y+h-t),
            (X(y+h)+t, y+h-t, X(y+h)+w-t, y+h), (X(m), m+t//2, X(y+h)+t, y+h-t), (X(y+t), y+t, X(m)+t, m-t//2),
            (X(m)+t, m-t//2, X(m)+w-t, m+t//2)]
    return [tuple(int(round(v)) for v in s) for s in segs]

def _cell(dark, c):
    vals = [float(dark[y0:y1, x0:x1].mean()) for (x0, y0, x1, y1) in _seg_boxes(c["x"], c["y"], c["w"], c["h"], c["t"], c["sk"])]
    segs = tuple(1 if v > c["thr"] else 0 for v in vals)
    return ("blank" if segs == (0,) * 7 else DIG.get(segs, "?")), segs, [round(v) for v in vals]

def read(path, crop, template):
    """Full pixel read. Returns a dict; 'ok' False with 'error' if the frame can't be trusted."""
    c = _calib()
    flat = np.asarray(flatten(path, crop, template), dtype=np.float32)
    ncc, dx, dy = register(flat)
    out = {"ok": True, "register": {"ncc": round(ncc, 3), "dx": dx, "dy": dy}, "scores": {}, "provisional": []}
    if ncc < c["min_ncc"]:
        out.update(ok=False, error=f"LCD does not match the reference (ncc {ncc:.2f}) - camera moved, dark, or screen off")
    a = np.roll(np.roll(flat, -dy, 0), -dx, 1)
    dark = c["_off"] - a
    for name, ic in c["icons"].items():
        x0, y0, x1, y1 = ic["box"]; v = float(dark[y0:y1, x0:x1].mean())
        out["scores"][name] = round(v, 1); out[name] = bool(v > ic["thr"])
        if ic.get("provisional"):
            out["provisional"].append(name)
    cells = [_cell(dark, cc) for cc in c["digits"]["cells"]]
    out["digit_cells"] = [{"digit": d, "segs": s, "values": v} for d, s, v in cells]
    digs = [d for d, _, _ in cells]
    if "?" in digs or digs[2] == "blank" or (digs[1] == "blank" and digs[0] != "blank"):
        out["temperature_f"] = None            # not a plain number: blank screen, or an error code like E04
    else:
        n = [0 if d == "blank" else d for d in digs]; out["temperature_f"] = n[0] * 100 + n[1] * 10 + n[2]
    out["mode"] = "dhw" if out.get("dhw_box") else ("heating" if out.get("radiator") else "idle")
    return out
