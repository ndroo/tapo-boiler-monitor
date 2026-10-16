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
BOILER_DISPLAY_CROP = (88, 172, 545, 300)   # the P-950EH LCD panel
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
- TOP ROW: SUN (far left) = warm-weather shutdown; HOUSE-with-thermometer = outdoor
  temperature mode (it FLASHES when no outdoor sensor is connected, so it carries no demand
  information); SNOWFLAKE = anti-freeze; a small BOX with HORIZONTAL LINES = "Appliance is
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
    Low confidence: only when the reading would CHANGE state versus the previous
    reading (flame / DHW / heating icons, or temperature by >= 15F), so an hourly
    'same as before, but blurry' frame doesn't double the cost."""
    if not isinstance(r, dict) or r.get("_error"):
        return ""
    if r.get("error_codes"):
        return "error code"
    if r.get("legible") is False:
        return "unreadable"
    if r.get("confidence") == "low":
        if not isinstance(prev, dict) or prev.get("_error") or prev.get("legible") is False:
            return "low confidence, no previous reading"
        changed = [k for k in STATE_KEYS if bool(r.get(k)) != bool(prev.get(k))]
        try:
            import re
            a = float(re.search(r"-?\d+", str(r.get("temperature"))).group()); b = float(re.search(r"-?\d+", str(prev.get("temperature"))).group())
            if abs(a - b) >= 15: changed.append("temperature")
        except Exception:
            pass
        if changed:
            return "low confidence + state change (" + ", ".join(changed) + ")"
    return ""


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
        imgs += [b64_image(p, width=FULL_FRAME_WIDTH), b64_image(p, crop=BOILER_DISPLAY_CROP, zoom=3)]
        reading, usage = analyze(client, model, BOILER_PROMPT, imgs, BOILER_SCHEMA)
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
        result["boiler"] = {"image": os.path.basename(p), "reading": reading, "usage": usage}
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
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    cap.DEBUG = a.debug

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
