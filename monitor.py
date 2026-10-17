#!/usr/bin/env python3
"""
monitor.py - poll both Tapo cameras once, save the stills, run each through Claude
vision for structured readings, and text an alert (via Twilio) if the boiler shows
any error code or its screen is unreadable.

  - boiler: the P-950EH controller LCD -> temperature, mode, flame, error codes
  - zone:   the Taco SR504-EXP panel   -> power + zones 1-4, read from PIXELS (white-core
            fraction at each LED's fixed position); Claude is only consulted when a LED is
            ambiguous or the power LED looks dark (panel off / camera moved)

Each cycle appends one JSON object to <out>/readings.jsonl and mirrors the latest to
<out>/readings_latest.json. Alert de-duplication state lives in <out>/alert_state.json
so a persistent fault texts you once (and once more when it clears), not every hour.

Env (local: .env file; CI: GitHub Secrets):
  ANTHROPIC_API_KEY                         required for analysis
  TPLINK_EMAIL / TPLINK_PASSWORD / TPLINK_TERMINAL_UUID   camera login
  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM / ALERT_TO   SMS (optional)

Usage:
  python3 monitor.py --once           # one cycle now (what CI runs)
  python3 monitor.py --interval 1800  # loop every 30 min in the foreground
  python3 monitor.py --model claude-haiku-5-5   # cheaper model
"""
import argparse, base64, json, os, sys, time, traceback
from datetime import datetime, timezone

import grab_boiler_cloud as cap  # reuse the capture pipeline (also loads .env)
from grab_boiler_cloud import load_dotenv
from PIL import Image
import requests
import anthropic

OUT_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stills")
MODEL_DEFAULT = "claude-opus-5-5"
# Second-opinion pass for the boiler: re-read with this model (medium effort) whenever the
# first pass is low-confidence, shows an error code, or says the screen is unreadable -
# i.e. exactly the readings that would trigger an alert or a state change. Benchmark on
# 15 local stills (2026-10-16): Sonnet 5.5 10/10 mode+temp+pumps, Opus 5.5 9/10 (one
# low-confidence false DHW), Haiku 5.5 7/10 mode + 3/10 pumps.
CONFIRM_MODEL_DEFAULT = "claude-opus-5-5"
ZONE_FALLBACK_MODEL = "claude-opus-5-5"   # Sonnet twice called a dark zone-1 LED lit; Opus/Haiku 10/10

# Fixed-view crops (x1,y1,x2,y2) used to give the model a zoomed view of the small
# indicators. Re-tune if a camera is moved.
ZONE_LED_CROP = (1040, 430, 1230, 640)      # Taco status LED column
BOILER_DISPLAY_CROP = (88, 172, 545, 300)   # the P-950EH LCD panel (reference position)
# The boiler camera gets nudged (it shifted ~250 px on 2026-10-17 and the fixed crop then
# showed only the left half of the LCD). locate_lcd() finds the panel in each frame by
# normalised cross-correlation against a template cut from a reference frame at
# BOILER_DISPLAY_CROP, and falls back to the fixed crop when the match is weak.
# Several templates may exist (docs/lcd_template*.png, one per camera position seen so
# far); the best-scoring one wins. Add a new one (the located crop, unpadded, grayscale)
# after a move if the match score drops to ~0.5.
LCD_TEMPLATE_GLOB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "lcd_template*.png")
LCD_MATCH_MIN = 0.45     # NCC score below this -> trust the fixed crop instead. Measured:
                         # same-position frames 0.89-1.0, after a ~270 px camera shift 0.53,
                         # a frame with no LCD at all (the zone panel) 0.37.
LCD_PAD = (28, 12)       # widen the located box by this much (x, y) on each side so a
                         # slightly larger/closer LCD is not clipped at the edges
PIXELS_MODE = os.environ.get("PIXELS_MODE", "on")   # on = pixel-first + model verifier; shadow = model reads, pixels logged; off = model only
FULL_FRAME_WIDTH = 640                      # context frame sent alongside the zoom (cheap)
LEGEND_IMAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "fig51_legend.png")

# Zone LEDs are read DETERMINISTICALLY from pixels: each LED's centre in the full frame
# and a radius. A lit LED has a blown-out white core (min(R,G,B) >= 200 over most of the
# disc); an unlit one is at most a dim colour bleed. Measured over 11 stills: ON = 0.81-0.96
# white fraction, OFF = 0.00-0.28 (zone 2 sitting between two lit LEDs is the worst case).
ZONE_LEDS = {"power": (1129, 462), "zone1": (1129, 487), "zone2": (1129, 510),
             "zone3": (1129, 534), "zone4": (1129, 558)}
LED_RADIUS = 8
LED_WHITE_MIN = 200          # per-channel floor for a "white core" pixel
LED_ON_FRAC = 0.5            # white fraction above this = ON
LED_AMBIGUOUS = (0.35, 0.65) # inside this band -> ask Claude to confirm

# HTP UFT-series boiler, P-950EH control. Icon legend is from manual LP-542 Figure 51
# (LCD Display). The model gets the annotated figure itself as a reference image.
BOILER_PROMPT = """You are reading the LCD of an HTP UFT-series boiler controller (P-950EH).
IMAGE 1 is the manufacturer's legend drawing of this exact LCD (manual LP-542, Figure 51)
with the key icons boxed. IMAGE 2 is the full camera frame for context. IMAGE 3 is a zoom
of the LCD - read from IMAGE 3, use IMAGE 1 to identify what each segment means.

Icon positions (as in the drawing, left to right):
- TOP ROW: SUN (far left) = warm-weather shutdown; HOUSE-with-thermometer = outdoor-reset
  (outdoor temperature) mode - normally lit on this boiler, carries no demand information;
  SNOWFLAKE = anti-freeze; a small BOX with HORIZONTAL LINES = "Appliance is
  Meeting DHW Demand" (this is the ONLY hot-water indicator); [i] = status/installer mode;
  WRENCH = service reminder; ((o)) = communication state.
- MIDDLE: time (AM/PM hh:mm) and day row on the left; a RADIATOR icon (vertical fins) just
  left of the big number = "Appliance is Operating in CH Mode" (heating demand); the big
  number = temperature with F or C.
- RIGHT, below the F/C letters: a CROWN-of-flames = burner firing (the only true flame
  indicator); a FAN icon = fan on; then TWO triangle-in-circle icons SIDE BY SIDE = Boiler
  pump (left) and CH/System pump (right); a THIRD triangle-in-circle ALONE BELOW them = DHW
  pump.
- BOTTOM: 0-18 numbered boxes = cascade status (normally blank on a single boiler).

Judge an icon LIT only if its segments are clearly, solidly dark/black. A faint ghost
outline (all segments are faintly visible on an LCD) is OFF. Assess each icon independently.
Report any fault code shown in place of the temperature (e.g. "E04", "Er:76") in
error_codes. If the screen is off or unreadable, set legible=false and all booleans false."""

BOILER_SCHEMA = {
    "type": "object",
    "properties": {
        "temperature":   {"type": ["string", "null"], "description": "big number with unit, e.g. '73°F'"},
        "time_text":     {"type": ["string", "null"], "description": "clock as shown, e.g. '10:07 AM'"},
        "day_text":      {"type": ["string", "null"], "description": "day-of-week segment lit, e.g. 'SUN'"},
        "flame_on":      {"type": "boolean"}, "fan_on": {"type": "boolean"},
        "ch_icon":       {"type": "boolean"}, "dhw_icon": {"type": "boolean"},
        "boiler_pump":   {"type": "boolean"}, "ch_pump": {"type": "boolean"}, "dhw_pump": {"type": "boolean"},
        "sun_icon":      {"type": "boolean"}, "house_icon": {"type": "boolean"}, "snowflake_icon": {"type": "boolean"},
        "info_icon":     {"type": "boolean"}, "wrench_icon": {"type": "boolean"}, "comm_icon": {"type": "boolean"},
        "cascade_text":  {"type": ["string", "null"], "description": "any cascade numbers/boxes lit, else null"},
        "error_codes":   {"type": "array", "items": {"type": "string"}},
        "display_text":  {"type": "string", "description": "everything legible on screen, verbatim"},
        "legible":       {"type": "boolean"},
        "confidence":    {"type": "string", "enum": ["high", "medium", "low"]},
        "notes":         {"type": "string", "description": "one short sentence on anything uncertain"},
    },
    "required": ["temperature", "time_text", "day_text", "flame_on", "fan_on", "ch_icon", "dhw_icon",
                 "boiler_pump", "ch_pump", "dhw_pump", "sun_icon", "house_icon", "snowflake_icon",
                 "info_icon", "wrench_icon", "comm_icon", "cascade_text", "error_codes", "display_text",
                 "legible", "confidence", "notes"],
    "additionalProperties": False,
}

ZONE_PROMPT = """These two images show a Taco SR504-EXP zone switching relay. The first
is the full scene; the second is a zoomed crop of the status LED column.

The LEDs, top to bottom, are: POWER, ZONE 1, ZONE 2, ZONE 3, ZONE 4/PRIORITY.

IMPORTANT: light bleeds between adjacent LEDs, so a faint coloured glow does NOT mean a
LED is on. A LED is ON only if it has a BRIGHT, near-white saturated CORE (the centre
looks white-hot / blown-out). A dim coloured dot with no bright core is OFF (that colour
is just bleed or ambient reflection). Report each LED."""

ZONE_SCHEMA = {
    "type": "object",
    "properties": {k: {"type": "boolean"} for k in ("power", "zone1", "zone2", "zone3", "zone4")},
    "required": ["power", "zone1", "zone2", "zone3", "zone4", "notes"],
    "additionalProperties": False,
}
ZONE_SCHEMA["properties"]["notes"] = {"type": "string"}


def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)


def b64_image(path, crop=None, zoom=1, width=None):
    """JPEG-encode a (cropped / zoomed / downscaled) view of an image as base64."""
    import io
    img = Image.open(path).convert("RGB")
    if crop:
        img = img.crop(crop)
    if zoom != 1:
        img = img.resize((img.width * zoom, img.height * zoom), Image.LANCZOS)
    if width and img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=90)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()


def _ncc_search(frame, tmpl, scale, centre=None, radius=0):
    """Best zero-mean normalised cross-correlation of tmpl over frame (both PIL 'L'),
    evaluated at 1/scale resolution. Returns (score, x, y) in that reduced frame."""
    import numpy as np
    f = np.asarray(frame.resize((frame.width // scale, frame.height // scale), Image.BILINEAR), dtype=np.float32)
    t = np.asarray(tmpl.resize((tmpl.width // scale, tmpl.height // scale), Image.BILINEAR), dtype=np.float32)
    t = t - t.mean(); tn = float(np.sqrt((t * t).sum())) or 1.0
    th, tw = t.shape
    if centre is None:
        xs, ys = range(0, f.shape[1] - tw + 1), range(0, f.shape[0] - th + 1)
    else:
        cx, cy = centre
        xs = range(max(0, cx - radius), min(f.shape[1] - tw, cx + radius) + 1)
        ys = range(max(0, cy - radius), min(f.shape[0] - th, cy + radius) + 1)
    best = (-2.0, 0, 0)
    for y in ys:
        rows = f[y:y + th]
        for x in xs:
            w = rows[:, x:x + tw]; w = w - w.mean(); d = float(np.sqrt((w * w).sum()))
            if d == 0: continue
            s = float((w * t).sum() / (d * tn))
            if s > best[0]: best = (s, x, y)
    return best


LCD_LAST = {"template": None}   # which template the last locate_lcd() matched (lcd_pixels needs it)


def _lcd_state_path(out): return os.path.join(out, "lcd_state.json")


def locate_lcd(path, out=None):
    """Find the P-950EH LCD in a boiler frame. Coarse NCC search over the whole frame at
    1/8 scale, then refine at full resolution. Returns (crop_box, score); the crop is the
    template's size at the best match, or BOILER_DISPLAY_CROP if the match is weak or the
    template/numpy are unavailable."""
    try:
        import glob
        import numpy  # noqa: F401
        frame = Image.open(path).convert("L")
        score, x, y, tmpl = -2.0, 0, 0, None
        for tp in sorted(glob.glob(LCD_TEMPLATE_GLOB)):
            t = Image.open(tp).convert("L")
            s8, x8, y8 = _ncc_search(frame, t, 8)
            s, tx, ty = _ncc_search(frame, t, 1, centre=(x8 * 8, y8 * 8), radius=10)
            if s > score:
                score, x, y, tmpl = s, tx, ty, t
                LCD_LAST["template"] = tp
        if tmpl is None:
            raise FileNotFoundError(LCD_TEMPLATE_GLOB)
        px, py = LCD_PAD
        crop = (max(0, x - px), max(0, y - py),
                min(frame.width, x + tmpl.width + px), min(frame.height, y + tmpl.height + py))
        if score < LCD_MATCH_MIN:
            log(f"boiler: LCD match weak ({score:.2f} at {crop[:2]}), using last good crop")
            return _last_good_crop(out), score
        if out:
            try:
                json.dump({"crop": list(crop), "match": round(score, 3), "path": os.path.basename(path)},
                          open(_lcd_state_path(out), "w"))
            except Exception:
                pass
        return crop, score
    except Exception as e:
        log("boiler: LCD locate failed, using last good crop:", e)
        return _last_good_crop(out), None


def _last_good_crop(out):
    """The crop from the last successful locate (survives a camera nudge even when numpy or
    the templates are missing); the original fixed crop only if there has never been one."""
    try:
        if out:
            c = json.load(open(_lcd_state_path(out)))["crop"]
            if len(c) == 4:
                return tuple(int(v) for v in c)
    except Exception:
        pass
    return BOILER_DISPLAY_CROP


def b64_file(path):
    """A file already on disk (PNG/JPEG), as (media_type, base64)."""
    mt = "image/png" if path.lower().endswith(".png") else "image/jpeg"
    return mt, base64.standard_b64encode(open(path, "rb").read()).decode()


def analyze(client, model, prompt, images, schema, effort="low"):
    """images: list of (media_type, base64). Structured output (JSON schema) so the reply
    is always valid JSON. Returns (dict, usage) - dict has _error on failure."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": mt, "data": d}}
               for mt, d in images]
    content.append({"type": "text", "text": prompt})
    usage = {}
    try:
        resp = client.messages.create(
            model=model, max_tokens=1024,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
        u = resp.usage
        usage = {"model": model, "input": u.input_tokens, "output": u.output_tokens,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0}
        if resp.stop_reason == "refusal":
            return {"_error": "refusal", "_detail": getattr(resp.stop_details, "category", None)}, usage
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        return json.loads(text), usage
    except json.JSONDecodeError:
        return {"_error": "unparseable", "_raw": text[:300]}, usage
    except Exception as e:
        return {"_error": type(e).__name__, "_detail": str(e)[:200]}, usage


def led_metrics(path):
    """Per-LED pixel stats from the full frame: fraction of 'white core' pixels and mean
    brightness inside a small disc at each LED's fixed position."""
    im = Image.open(path).convert("RGB"); px = im.load(); out = {}
    r2 = LED_RADIUS * LED_RADIUS
    for name, (cx, cy) in ZONE_LEDS.items():
        n = w = 0; tot = 0.0
        for x in range(cx - LED_RADIUS, cx + LED_RADIUS + 1):
            for y in range(cy - LED_RADIUS, cy + LED_RADIUS + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 > r2: continue
                if not (0 <= x < im.width and 0 <= y < im.height): continue
                r, g, b = px[x, y]; n += 1; tot += (r + g + b) / 3
                if min(r, g, b) >= LED_WHITE_MIN: w += 1
        out[name] = {"white": round(w / n, 3) if n else 0.0, "mean": round(tot / n) if n else 0}
    return out


def zone_from_pixels(m):
    """Deterministic LED verdicts + whether any LED is in the ambiguous band."""
    reading = {k: m[k]["white"] >= LED_ON_FRAC for k in ZONE_LEDS}
    lo, hi = LED_AMBIGUOUS
    ambiguous = [k for k in ZONE_LEDS if lo <= m[k]["white"] <= hi]
    return reading, ambiguous


# ------------------------------------------------------------------ alerting
def send_sms(body):
    """Send an SMS via Twilio's REST API (no SDK needed). No-op if env not set.

    Supports either auth style:
      - Account SID (AC...) + Auth Token, or
      - an API Key (SK...) + its secret  (TWILIO_API_KEY_SID / TWILIO_API_KEY_SECRET).
    TWILIO_ACCOUNT_SID (the AC... id) is always required - it's in the request URL."""
    account = os.environ.get("TWILIO_ACCOUNT_SID")                       # AC... -> URL path
    user = os.environ.get("TWILIO_API_KEY_SID") or account              # SK... or AC...
    pw = os.environ.get("TWILIO_API_KEY_SECRET") or os.environ.get("TWILIO_AUTH_TOKEN")
    frm = os.environ.get("TWILIO_FROM")
    to = os.environ.get("ALERT_TO")
    if not all([account, user, pw, frm, to]):
        log("SMS not sent (Twilio env not fully set). Would have sent:", body)
        return {"status": "not_configured", "message": body}
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account}/Messages.json",
            data={"From": frm, "To": to, "Body": body[:1500]},
            auth=(user, pw), timeout=20,
        )
        if r.status_code < 300:
            sid = ""
            try: sid = r.json().get("sid", "")
            except Exception: pass
            log("SMS sent:", body)
            return {"status": "sent", "sid": sid, "message": body}
        log(f"SMS FAILED (HTTP {r.status_code}): {r.text[:200]}")
        return {"status": "failed", "error": f"HTTP {r.status_code}: {r.text[:160]}", "message": body}
    except Exception as e:
        log("SMS error:", e)
        return {"status": "failed", "error": str(e)[:200], "message": body}


TEMP_MIN_F, TEMP_MAX_F = 40, 230   # plausible P-950EH supply-temperature display range


def temp_f(t):
    """Numeric degrees from the model's temperature string ('113°F', '73 F'), else None."""
    import re
    m = re.search(r"-?\d+", str(t or ""))
    return float(m.group()) if m else None


def derive_mode(r):
    """Deterministically map the individual icon booleans to a single mode label.
    Flame is reported separately (flame_on); a mode can be active with the burner
    between cycles. Priority: dhw > heating > idle."""
    if not isinstance(r, dict) or r.get("legible") is False:
        return "unknown"
    if r.get("dhw_icon"):
        return "dhw"
    if r.get("ch_icon"):
        return "heating"
    return "idle"


def boiler_problems(reading):
    """Return a list of human-readable problem strings for the boiler reading."""
    probs = []
    if not isinstance(reading, dict) or reading.get("_error"):
        return probs  # AI/parse hiccup is not a boiler fault; capture-fail is handled separately
    codes = reading.get("error_codes") or []
    codes = [str(c).strip() for c in codes if str(c).strip()]
    if codes:
        probs.append("error code(s): " + ", ".join(codes))
    if reading.get("legible") is False:
        probs.append("screen unreadable / display out")
    if reading.get("lcd_unregistered"):
        probs.append("boiler LCD not recognised - camera moved or screen off")
    if any(f in ALERT_ON_PIXEL_MISS for f in (reading.get("pixel_miss") or [])):
        probs.append("pixel read disagreed with the model on " + ", ".join(f for f in reading["pixel_miss"] if f in ALERT_ON_PIXEL_MISS))
    elif reading.get("confidence") == "low":
        # Still low after the confirmation pass: the readings themselves are suspect
        # (camera moved, glare, lens dirty, display dim) - worth a text.
        probs.append("low-confidence reading - check the boiler camera view")
    return probs


def log_alert(out, result, kind, message, problems, delivery, sid="", error=""):
    """Append one line to alerts.jsonl - the durable, auditable alert history.
    kind: problem | cleared | suppressed. delivery: sent | failed | not_configured | suppressed."""
    rec = {"timestamp": result.get("timestamp"), "local": result.get("local"),
           "kind": kind, "delivery": delivery, "message": message, "problems": problems}
    if sid: rec["sid"] = sid
    if error: rec["error"] = error
    with open(os.path.join(out, "alerts.jsonl"), "a") as f:
        f.write(json.dumps(rec) + "\n")


def _state_path(out): return os.path.join(out, "alert_state.json")


def load_state(out):
    try:
        return json.load(open(_state_path(out)))
    except Exception:
        return {"sig": "", "cap_fail": 0}


def save_state(out, state):
    with open(_state_path(out), "w") as f:
        json.dump(state, f, indent=2)


# Don't text about a failure more than once per 24h... unless an OK ("pass") cycle
# happened in between, in which case a fresh failure re-alerts right away.
ALERT_MIN_INTERVAL = 24 * 3600


def evaluate_and_alert(result, out):
    """Decide if anything is wrong with the boiler and text, respecting the 24h /
    pass-between-failures throttle."""
    state = load_state(out)
    boiler = result.get("boiler", {})
    probs = boiler_problems(boiler.get("reading", {}))

    # Track consecutive complete capture failures (camera unreachable / stream dead).
    if boiler.get("_error") and "reading" not in boiler:
        state["cap_fail"] = state.get("cap_fail", 0) + 1
    else:
        state["cap_fail"] = 0
    if state["cap_fail"] >= 3:
        probs.append(f"boiler camera unreachable for {state['cap_fail']} cycles")

    sig = " | ".join(sorted(probs))
    when = result.get("local", "")
    now = time.time()
    sent = False

    if sig:  # something is wrong
        last_ts = state.get("last_alert_ts")
        passed = state.get("pass_since_alert", False)   # an OK cycle since the last alert
        stale = last_ts is not None and (now - last_ts) >= ALERT_MIN_INTERVAL
        if last_ts is None or passed or stale:
            msg = f"⚠️ Boiler monitor: {sig}. ({when})"
            res = send_sms(msg)
            sent = res.get("status") == "sent"
            log_alert(out, result, "problem", msg, probs, res.get("status"), res.get("sid", ""), res.get("error", ""))
            state["last_alert_ts"] = now
            state["alerted_open"] = True
            state["pass_since_alert"] = False
        else:
            log(f"alert suppressed (within 24h, no pass since last): {sig}")
            log_alert(out, result, "suppressed", f"⚠️ {sig}", probs, "suppressed")
    else:    # OK cycle == a "pass"
        if state.get("alerted_open"):
            msg = f"✅ Boiler monitor: previous issue cleared, now normal. ({when})"
            res = send_sms(msg)
            log_alert(out, result, "cleared", msg, [], res.get("status"), res.get("sid", ""), res.get("error", ""))
        state["alerted_open"] = False
        state["pass_since_alert"] = True

    state["sig"] = sig
    save_state(out, state)
    result["alert"] = {"problems": probs, "sent": sent, "throttled": bool(sig) and not sent}


STATE_KEYS = ("flame_on", "dhw_icon", "ch_icon")


def needs_confirmation(r, prev=None):
    """Why (or '' if not) a boiler reading deserves a second-model read.
    Always: an error code or an unreadable screen (both would alert).
    Low confidence: always (a reading that stays low-confidence after this pass
    raises an alert, so the stronger model gets the first look)."""
    if not isinstance(r, dict) or r.get("_error"):
        return ""
    if r.get("error_codes"):
        return "error code"
    if r.get("legible") is False:
        return "unreadable"
    if r.get("confidence") == "low":
        # Low confidence now alerts (boiler_problems), so always give the stronger model a
        # chance to resolve it first.
        return "low confidence"
    return ""


# ---------------------------------------------------------------------------------------
# Pixel-first boiler reading, with the model standing in for a human.
#   - lcd_pixels reads mode / burner / fan / pumps / temperature from pixels every cycle.
#   - The model (Sonnet) is asked to look only when there is a reason: the digits don't decode
#     (error code?), the LCD didn't register (camera moved / screen off), a never-confirmed
#     icon lit, the state changed, the temperature jumped, a field hasn't earned trust yet,
#     or it's time for a periodic audit. Its answer is compared field by field; a disagreement
#     goes to the confirmation model (Opus) as arbiter.
#   - Trust: consecutive agreements per field in <out>/lcd_trust.json. Once a field has
#     TRUST_FULL agreements the model is no longer consulted for it routinely, only on the
#     triggers above. A disagreement resets that field's streak.
# ---------------------------------------------------------------------------------------
PIXEL_FIELDS = ("mode", "flame_on", "boiler_pump", "ch_pump", "temperature")   # fan_on is pixel-read but not compared: the models misread it
TRUST_FULL = 200          # agreements in a row before routine model checks stop for a field
AUDIT_SECONDS = 2 * 3600  # trusted or not, ask the model at least this often
TEMP_JUMP_F = 20          # a pixel temperature move bigger than this between cycles gets a model look
PX_FIELD_MAP = {"flame_on": "flame", "fan_on": "fan", "boiler_pump": "boiler_pump", "ch_pump": "ch_pump",
                "dhw_pump": "dhw_pump", "dhw_icon": "dhw_box", "sun_icon": "sun", "snowflake_icon": "snowflake",
                "info_icon": "info", "wrench_icon": "wrench", "house_icon": "house", "comm_icon": "comm"}
PROVISIONAL_FIELDS = {"dhw_icon": "dhw_box", "dhw_pump": "dhw_pump", "sun_icon": "sun", "snowflake_icon": "snowflake",
                      "info_icon": "info", "wrench_icon": "wrench"}
ALERT_ON_PIXEL_MISS = ("mode", "temperature")   # arbiter siding with the model on these -> alert


def _trust_path(out): return os.path.join(out, "lcd_trust.json")


def load_trust(out):
    try:
        t = json.load(open(_trust_path(out)))
    except Exception:
        t = {}
    t.setdefault("fields", {}); t.setdefault("learned_icons", {}); t.setdefault("cycles", 0)
    t.setdefault("model_calls", 0); t.setdefault("disagreements", [])
    return t


def save_trust(out, t):
    t["disagreements"] = t["disagreements"][-50:]
    json.dump(t, open(_trust_path(out), "w"), indent=1)


def px_as_reading(px):
    """Pixel read -> the reading schema the dashboard and alerts already use."""
    r = {k: bool(px.get(v)) for k, v in PX_FIELD_MAP.items()}
    t = px.get("temperature_f")
    r["temperature"] = f"{t}°F" if t is not None else None
    r["ch_icon"] = bool(px.get("radiator"))
    r.update(error_codes=[], display_text="", time_text=None, day_text=None, cascade_text=None, notes="",
             legible=bool(px.get("ok")), confidence="medium")
    r["pump_on"] = bool(r["boiler_pump"] or r["ch_pump"] or r["dhw_pump"])
    r["mode"] = derive_mode(r)
    return r


def verify_reasons(px, cur, prev, trust):
    """Why the model should look at this frame (empty list = no need)."""
    reasons = []
    if not px.get("ok"):
        reasons.append("LCD not registered (camera moved / screen off)")
    elif px.get("temperature_f") is None:
        reasons.append("digits do not decode (error code?)")
    lit_prov = [n for n in px.get("provisional", []) if px.get(n)]
    if lit_prov:
        reasons.append("unconfirmed icon lit: " + ", ".join(lit_prov))
    untrusted = [f for f in PIXEL_FIELDS if trust["fields"].get(f, {}).get("streak", 0) < TRUST_FULL]
    if untrusted:
        reasons.append(f"building trust ({len(untrusted)} field(s) under {TRUST_FULL} agreements)")
    if isinstance(prev, dict):
        changed = [k for k in ("mode", "flame_on", "boiler_pump", "ch_pump") if prev.get(k) != cur.get(k)]
        if changed:
            reasons.append("state change: " + ", ".join(changed))
        a, b = temp_f(cur.get("temperature")), temp_f(prev.get("temperature"))
        if a is not None and b is not None and abs(a - b) > TEMP_JUMP_F:
            reasons.append(f"temperature jump {b:.0f}->{a:.0f} F")
    if time.time() - (trust.get("last_model_check_ts") or 0) >= AUDIT_SECONDS:
        reasons.append("periodic audit")
    return reasons


def _field_equal(f, a, b):
    if f == "temperature":
        x, y = temp_f(a), temp_f(b)
        return x is not None and y is not None and abs(x - y) <= 1
    return bool(a) == bool(b)


def read_boiler(client, model, confirm_model, out, p, lcd_crop, lcd_score, imgs_base):
    """Pixel-first reading verified by the model. Returns (reading, usage, px, trust_summary)."""
    import lcd_pixels
    trust = load_trust(out); trust["cycles"] += 1
    usage = {"model": None, "input": 0, "output": 0, "cache_read": 0}
    px = lcd_pixels.read(p, lcd_crop, LCD_LAST.get("template"), learned=trust["learned_icons"])
    cur = px_as_reading(px)
    prev = previous_boiler_reading(out)
    reasons = verify_reasons(px, cur, prev, trust)
    cur["source"] = {"pixels": list(PIXEL_FIELDS), "model": [], "reasons": reasons, "verified_by": None, "arbiter": None}
    if not reasons:
        cur["confidence"] = "high" if px.get("ok") else "low"
        cur["notes"] = "pixels only (trusted); model not consulted"
        save_trust(out, trust)
        return cur, usage, px, _trust_summary(trust)

    # --- the model looks (with a reference frame of the same screen in normal operation) ---
    imgs = list(imgs_base)
    ref_png = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "lcd_flat_ref.png")
    if os.path.exists(ref_png):
        imgs.append(b64_file(ref_png))
    prompt = BOILER_PROMPT + ("\n\nIMAGE 4 is this same LCD photographed earlier in normal operation, flattened, for "
                              "comparison only (its values are stale). Use it to judge whether anything unusual - an "
                              "error code such as E04 / Er:76 in place of the temperature, a service wrench, a blank "
                              "or partial display - is showing in the current frame.")
    m, u = analyze(client, model, prompt, imgs, BOILER_SCHEMA)
    trust["model_calls"] += 1; trust["last_model_check_ts"] = time.time()
    for k in ("input", "output", "cache_read"):
        usage[k] += u.get(k) or 0
    usage["model"] = model
    if not isinstance(m, dict) or m.get("_error"):
        cur["notes"] = "model check failed (" + str((m or {}).get("_error")) + "); pixels unverified: " + "; ".join(reasons)
        cur["confidence"] = "medium" if px.get("ok") else "low"
        save_trust(out, trust)
        return cur, usage, px, _trust_summary(trust)
    m["mode"] = derive_mode(m)
    cur["source"]["verified_by"] = model

    # If the LCD didn't register at all, the model's reading is the reading (old behaviour).
    if not px.get("ok"):
        m["pump_on"] = bool(m.get("boiler_pump") or m.get("ch_pump") or m.get("dhw_pump"))
        m["lcd_unregistered"] = px.get("error")
        m["source"] = {"pixels": [], "model": "all", "reasons": reasons, "verified_by": model, "arbiter": None}
        m["notes"] = (m.get("notes") or "") + " | pixels: " + str(px.get("error"))
        save_trust(out, trust)
        return m, usage, px, _trust_summary(trust)

    # Fields only the model can give.
    for k in ("error_codes", "display_text", "time_text", "day_text", "cascade_text"):
        cur[k] = m.get(k)
    cur["source"]["model"] = ["error_codes", "display_text", "time_text", "day_text"]
    if px.get("temperature_f") is None:
        cur["temperature"] = m.get("temperature"); cur["source"]["model"].append("temperature")
        cur["legible"] = bool(m.get("legible", True))

    # Never-confirmed icons: pixels propose, the model confirms; a confirmation promotes the box.
    # A model-only claim (pixels off) needs the arbiter too - the model has false-positived on DHW before.
    arbiter_q = []          # things the arbiter must rule on this cycle
    prov_model_only = []
    for fld, icon in PROVISIONAL_FIELDS.items():
        if icon not in px.get("provisional", []):
            continue
        px_lit, m_lit = bool(px.get(icon)), bool(m.get(fld))
        if px_lit and m_lit:
            thr = (px["scores"][icon] + (lcd_pixels._calib()["icons"][icon].get("off_max") or 0)) / 2
            trust["learned_icons"][icon] = {"thr": round(thr, 1), "score": px["scores"][icon],
                                            "confirmed": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": model}
            cur["notes"] += f" | {icon} icon confirmed lit by {model} -> box promoted"
        elif px_lit and not m_lit:
            cur[fld] = False
            cur["notes"] += f" | {icon}: pixels lit but {model} says off (reported off; box still unconfirmed)"
        elif m_lit and not px_lit:
            prov_model_only.append((fld, icon)); arbiter_q.append(fld)
    # An error code claimed by the model while the digits decode as a plain number is suspect.
    if cur.get("error_codes") and px.get("temperature_f") is not None:
        arbiter_q.append("error_codes")
    cur["pump_on"] = bool(cur["boiler_pump"] or cur["ch_pump"] or cur["dhw_pump"]); cur["mode"] = derive_mode(cur)

    # Compare the fields pixels own; escalate disagreements to the arbiter.
    disagree = [f for f in PIXEL_FIELDS if f not in cur["source"]["model"] and not _field_equal(f, cur.get(f), m.get(f))]
    arbiter_q += disagree
    cur["confidence"] = "high"
    verdict = {}
    if arbiter_q and confirm_model:
        log(f"boiler: arbiter {confirm_model} asked about {arbiter_q}")
        arb, u2 = analyze(client, confirm_model, prompt, imgs, BOILER_SCHEMA, effort="medium")
        for k in ("input", "output", "cache_read"):
            usage[k] += u2.get(k) or 0
        usage["confirm_model"] = confirm_model
        if isinstance(arb, dict) and not arb.get("_error"):
            arb["mode"] = derive_mode(arb)
            px_vals = px_as_reading(px)
            for f in disagree:
                with_px = _field_equal(f, cur.get(f), arb.get(f)); verdict[f] = "pixels" if with_px else "model"
                if not with_px:
                    cur[f] = m.get(f); cur["source"]["model"].append(f)
                    cur.setdefault("pixel_miss", []).append(f)
                    if f in ALERT_ON_PIXEL_MISS:
                        cur["confidence"] = "low"
            for fld, icon in prov_model_only:
                if bool(arb.get(fld)):
                    cur[fld] = True; verdict[fld] = "model"
                    cur["notes"] += f" | {icon}: both models say lit but the pixel box (score {px['scores'][icon]}) does not - box likely misplaced, needs recalibration"
                else:
                    cur[fld] = False; verdict[fld] = "pixels"
            if "error_codes" in arbiter_q:
                if arb.get("error_codes"):
                    verdict["error_codes"] = "model"; cur["error_codes"] = sorted(set(cur["error_codes"]) | set(arb["error_codes"]))
                else:
                    verdict["error_codes"] = "pixels"; cur["notes"] += f" | {model} claimed error code {cur['error_codes']} but digits decode and {confirm_model} sees none - dropped"; cur["error_codes"] = []
            cur["source"]["arbiter"] = {"model": confirm_model, "verdict": verdict}
            if disagree:
                trust["disagreements"].append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "image": os.path.basename(p),
                                               "fields": {f: {"pixels": px_vals.get(f), "model": m.get(f), "verdict": verdict[f]} for f in disagree}})
                cur["notes"] += " | disagreement on " + ", ".join(f"{f} (arbiter: {verdict[f]})" for f in disagree)
        else:
            cur["confidence"] = "low"; cur["notes"] += " | arbiter failed on " + ", ".join(arbiter_q)
            for fld, icon in prov_model_only:
                cur[fld] = False
    elif disagree:
        cur["confidence"] = "low"; cur["notes"] += " | disagreement on " + ", ".join(disagree) + " (no arbiter)"
    # Trust: a field's streak resets only when the pixels were shown wrong (arbiter sided with the model).
    for f in PIXEL_FIELDS:
        st = trust["fields"].setdefault(f, {"streak": 0, "agree": 0, "disagree": 0})
        if f in disagree and verdict.get(f) != "pixels":
            st["streak"] = 0; st["disagree"] += 1
        else:
            st["streak"] += 1; st["agree"] += 1
    cur["pump_on"] = bool(cur["boiler_pump"] or cur["ch_pump"] or cur["dhw_pump"]); cur["mode"] = derive_mode(cur)
    if cur.get("error_codes"):
        cur["confidence"] = "high"   # an error code read by the model is a finding, not an uncertainty
    cur["notes"] = ("pixels + " + model + " (" + "; ".join(reasons) + ")" + cur["notes"]).strip()
    save_trust(out, trust)
    return cur, usage, px, _trust_summary(trust)


def _trust_summary(t):
    return {"cycles": t["cycles"], "model_calls": t["model_calls"],
            "streaks": {f: t["fields"].get(f, {}).get("streak", 0) for f in PIXEL_FIELDS},
            "learned_icons": sorted(t["learned_icons"])}


def previous_boiler_reading(out):
    try:
        return json.load(open(os.path.join(out, "readings_latest.json"))).get("boiler", {}).get("reading")
    except Exception:
        return None


def cycle(client, model, out, zone_llm="auto", confirm_model=CONFIRM_MODEL_DEFAULT):
    os.makedirs(out, exist_ok=True)
    result = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "local": datetime.now().isoformat(timespec="seconds")}

    # --- Boiler: legend drawing + small context frame + 3x LCD zoom -> Claude (structured) ---
    try:
        p = cap.grab_once(out, cap.CAMERAS["boiler"], name="boiler", opts={"frames": 8, "sharpen": True, "quality": 95})
        imgs = []
        if os.path.exists(LEGEND_IMAGE):
            imgs.append(b64_file(LEGEND_IMAGE))
        lcd_crop, lcd_score = locate_lcd(p, out)
        imgs += [b64_image(p, width=FULL_FRAME_WIDTH), b64_image(p, crop=lcd_crop, zoom=3)]
        px = trust_summary = None
        if PIXELS_MODE == "on":
            try:
                reading, usage, px, trust_summary = read_boiler(client, model, confirm_model, out, p, lcd_crop, lcd_score, imgs)
                log("boiler pixels:", json.dumps({k: px.get(k) for k in ("mode", "flame", "fan", "boiler_pump", "ch_pump", "temperature_f", "ok", "register")}))
                log("boiler trust:", json.dumps(trust_summary))
            except Exception as e:
                log("boiler: pixel path failed, falling back to the model:", repr(e))
                px = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
                reading, usage = analyze(client, model, BOILER_PROMPT, imgs, BOILER_SCHEMA)
        else:
            reading, usage = analyze(client, model, BOILER_PROMPT, imgs, BOILER_SCHEMA)
        why = ""
        if px is None or not px.get("ok"):
            why = needs_confirmation(reading, previous_boiler_reading(out)) if confirm_model else ""
        if isinstance(reading, dict) and reading.get("confidence") == "low" and not why:
            log(f"boiler: first pass ({model}) low confidence but same state as last reading -> no confirmation")
        if why:
            log(f"boiler: first pass ({model}): {why} -> confirming with {confirm_model}")
            second, u2 = analyze(client, confirm_model, BOILER_PROMPT, imgs, BOILER_SCHEMA, effort="medium")
            if isinstance(second, dict) and not second.get("_error"):
                second["first_pass"] = {"model": model, "why_confirmed": why,
                                        **{k: reading.get(k) for k in ("temperature", "confidence", "error_codes", "legible", "dhw_icon", "ch_icon", "flame_on")}}
                reading = second
            for k in ("input", "output", "cache_read"):
                usage[k] = (usage.get(k) or 0) + (u2.get(k) or 0)
            usage["confirm_model"] = confirm_model
        if isinstance(reading, dict) and not reading.get("_error"):
            reading["pump_on"] = bool(reading.get("boiler_pump") or reading.get("ch_pump") or reading.get("dhw_pump"))
            reading["mode"] = derive_mode(reading)
            t = temp_f(reading.get("temperature"))
            if t is not None and not (TEMP_MIN_F <= t <= TEMP_MAX_F):
                # A clipped or misread digit (e.g. "10" for 106) - keep the text, drop the number.
                log(f"boiler: temperature {reading.get('temperature')!r} outside {TEMP_MIN_F}-{TEMP_MAX_F} F -> suspect")
                reading["temperature_suspect"] = reading.get("temperature")
                reading["temperature"] = None
                reading["confidence"] = "low"
        result["boiler"] = {"image": os.path.basename(p), "reading": reading, "usage": usage,
                            "lcd": {"crop": list(lcd_crop), "match": None if lcd_score is None else round(lcd_score, 3)}}
        if PIXELS_MODE == "shadow":
            try:
                import lcd_pixels
                px = lcd_pixels.read(p, lcd_crop, LCD_LAST.get("template"))
            except Exception as e:
                px = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        if px is not None:
            result["boiler"]["pixels"] = {k: px.get(k) for k in ("ok", "error", "mode", "flame", "fan", "boiler_pump", "ch_pump", "dhw_box", "dhw_pump", "temperature_f", "register", "scores", "provisional") if k in px}
        if trust_summary:
            result["boiler"]["trust"] = trust_summary
        log("boiler:", json.dumps(reading))
    except Exception as e:
        result["boiler"] = {"_error": str(e)}
        log("boiler capture failed:", e)

    # --- Zone controller: pixels first; Claude only if ambiguous / panel dark / forced ---
    try:
        p = cap.grab_once(out, cap.CAMERAS["zone"], name="zone", opts={"frames": 8, "sharpen": False, "quality": 95})
        m = led_metrics(p)
        px_reading, ambiguous = zone_from_pixels(m)
        need_llm = (zone_llm == "always") or (zone_llm == "auto" and (ambiguous or not px_reading["power"]))
        entry = {"image": os.path.basename(p), "pixels": m}
        if need_llm:
            imgs = [b64_image(p, width=FULL_FRAME_WIDTH), b64_image(p, crop=ZONE_LED_CROP, zoom=4)]
            llm, usage = analyze(client, ZONE_FALLBACK_MODEL, ZONE_PROMPT, imgs, ZONE_SCHEMA)
            entry["usage"] = usage
            if isinstance(llm, dict) and not llm.get("_error"):
                llm["method"] = "claude"
                llm["pixel_verdict"] = px_reading
                llm["why_llm"] = ("ambiguous:" + ",".join(ambiguous)) if ambiguous else ("forced" if zone_llm == "always" else "power LED dark")
                entry["reading"] = llm
            else:
                px_reading["method"] = "pixels"; px_reading["notes"] = "Claude fallback failed: " + str(llm)
                entry["reading"] = px_reading
        else:
            px_reading["method"] = "pixels"
            px_reading["notes"] = "white-core fraction per LED: " + ", ".join(f"{k} {m[k]['white']:.2f}" for k in ZONE_LEDS)
            entry["reading"] = px_reading
        result["zone"] = entry
        log("zone:  ", json.dumps(entry["reading"]))
    except Exception as e:
        result["zone"] = {"_error": str(e)}
        log("zone capture failed:", e)

    evaluate_and_alert(result, out)

    with open(os.path.join(out, "readings.jsonl"), "a") as f:
        f.write(json.dumps(result) + "\n")
    with open(os.path.join(out, "readings_latest.json"), "w") as f:
        json.dump(result, f, indent=2)
    return result


def main():
    global PIXELS_MODE
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--interval", type=int, default=3600, help="seconds between cycles (default 3600 = 1h)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--model", default=MODEL_DEFAULT)
    ap.add_argument("--confirm-model", default=CONFIRM_MODEL_DEFAULT,
                    help="re-read low-confidence / error / unreadable boiler frames with this model "
                         "(medium effort); '' disables")
    ap.add_argument("--zone-llm", choices=["auto", "always", "never"], default="auto",
                    help="zone LEDs: 'auto' = pixels, Claude only when ambiguous (default); "
                         "'always' = also ask Claude every cycle; 'never' = pixels only")
    ap.add_argument("--pixels", choices=["on", "shadow", "off"], default=PIXELS_MODE,
                    help="on: pixels read the LCD and the model verifies (default); shadow: model reads, pixels logged; off: model only")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    cap.DEBUG = a.debug
    PIXELS_MODE = a.pixels

    load_dotenv()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("No ANTHROPIC_API_KEY found. Set it in the environment, in a local .env, "
                 "or (in CI) as a GitHub Secret, then re-run.")
    client = anthropic.Anthropic()

    log(f"monitor started (model={a.model}, interval={a.interval}s, out={a.out})")
    while True:
        try:
            cycle(client, a.model, a.out, zone_llm=a.zone_llm, confirm_model=a.confirm_model or None)
        except Exception:
            log("cycle error:\n" + traceback.format_exc())
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
