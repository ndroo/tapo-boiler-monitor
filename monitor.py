#!/usr/bin/env python3
"""
monitor.py - poll both Tapo cameras once, save the stills, run each through Claude
vision for structured readings, and text an alert (via Twilio) if the boiler shows
any error code or its screen is unreadable.

  - boiler: the P-950EH controller LCD -> temperature, mode, flame, error codes
  - zone:   the Taco SR504-EXP panel   -> power + zones 1/2/3 on/off (bright LED core)

Each cycle appends one JSON object to <out>/readings.jsonl and mirrors the latest to
<out>/readings_latest.json. Alert de-duplication state lives in <out>/alert_state.json
so a persistent fault texts you once (and once more when it clears), not every hour.

Env (local: .env file; CI: GitHub Secrets):
  ANTHROPIC_API_KEY                         required for analysis
  TPLINK_EMAIL / TPLINK_PASSWORD / TPLINK_TERMINAL_UUID   camera login
  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM / ALERT_TO   SMS (optional)

Usage:
  python3 monitor.py --once           # one cycle now (what CI runs)
  python3 monitor.py --interval 3600  # loop hourly in the foreground
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

# Fixed-view crops (x1,y1,x2,y2) used to give the model a zoomed view of the small
# indicators. Re-tune if a camera is moved.
ZONE_LED_CROP = (1040, 430, 1230, 640)      # Taco status LED column
BOILER_DISPLAY_CROP = (88, 172, 545, 300)   # the P-950EH LCD panel

# HTP UFT-series boiler, P-950EH control. Icon legend is from manual LP-542 Figure 51
# (LCD Display) - positions are exactly as they appear on the physical display.
BOILER_PROMPT = """These two images are a camera view of an HTP UFT-series boiler
controller LCD (P-950EH control; the second image is a zoom). Read the LCD using this
EXACT icon legend; the stated positions are where each icon sits on the display:

- CH / central-heating demand: a RADIATOR icon (a block of several vertical bars/fins
  with short horizontal lines), on the LEFT, just left of the big number. (It flashes
  when there is a call for heat while a DHW demand is also being met.)
- DHW (domestic hot water) demand: a small RECTANGLE / BOX filled with a few HORIZONTAL
  LINES, in the TOP ROW, sitting between the snowflake and the [i] info icon. This box is
  "Appliance is Meeting DHW Demand". (It flashes if there's a hot-water call plus a CH
  demand.) There is NO separate shower or tank icon on this model - this box is DHW.
- FLAME ON (burner actually firing): a CROWN-OF-FLAMES icon (a row of ~4-5 pointed peaks,
  like a small crown/comb), on the RIGHT side, below the F/C letters. This CROWN is the
  ONLY true "is the burner firing" indicator - do not infer flame from anything else.
- FAN ON: a small FAN icon on the RIGHT, just below the flame crown.
- Pumps (three TRIANGLE-IN-CIRCLE icons stacked on the lower RIGHT, top to bottom =
  Boiler pump, CH/System pump, DHW pump). Report whether any pump triangle is lit, and
  the DHW (bottom) one specifically if you can tell them apart.
- Outdoor-temperature mode: a HOUSE-WITH-THERMOMETER icon, upper-left. It FLASHES when no
  outdoor sensor is connected, so it usually means only "no sensor", not any demand - IGNORE
  it for mode. A SUN icon (far upper-left) = warm-weather shutdown. SNOWFLAKE = anti-freeze.
- Also present (note only in display_text if clearly lit): [i] info (top), wrench (service),
  ((o)) communication (top right). Bottom row 0-18 boxes = cascade status.

Judge an icon as LIT only if its segments are clearly, solidly on - a faint ghost = OFF.
Assess each icon independently. Return ONLY a JSON object:
{
  "temperature": "<the big number with units, or null>",
  "flame_on": true/false,     // the CROWN-of-flames icon on the right is solidly lit
  "fan_on": true/false,       // the fan icon on the right
  "ch_icon": true/false,      // radiator icon (left) = heating demand
  "dhw_icon": true/false,     // top-row horizontal-lines box = DHW demand
  "pump_on": true/false,      // any triangle-in-circle pump icon lit (lower right)
  "dhw_pump": true/false,     // the DHW (bottom) pump triangle specifically, if distinguishable
  "error_codes": ["<any fault codes shown, e.g. E04>"],
  "display_text": "<any other legible text/number on screen, verbatim>",
  "legible": true/false
}
If the screen is off or unreadable, set legible=false and all icon booleans false."""

ZONE_PROMPT = """These two images show a Taco SR504-EXP zone switching relay. The first
is the full scene; the second is a zoomed crop of the status LED column.

The LEDs, top to bottom, are: POWER, ZONE 1, ZONE 2, ZONE 3, ZONE 4/PRIORITY.

IMPORTANT: light bleeds between adjacent LEDs, so a faint coloured glow does NOT mean a
LED is on. A LED is ON only if it has a BRIGHT, near-white saturated CORE (the centre
looks white-hot / blown-out). A dim coloured dot with no bright core is OFF (that colour
is just bleed or ambient reflection).

Report ONLY whether POWER and ZONE 1/2/3 are on. Return ONLY a JSON object:
{
  "power": true/false,
  "zone1": true/false,
  "zone2": true/false,
  "zone3": true/false,
  "notes": "<brief: which LEDs had a bright core vs only bleed>"
}"""


def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)


def b64_image(path, crop=None, zoom=1):
    img = Image.open(path).convert("RGB")
    if crop:
        img = img.crop(crop)
    if zoom != 1:
        img = img.resize((img.width * zoom, img.height * zoom))
    import io
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=92)
    return base64.standard_b64encode(buf.getvalue()).decode()


def analyze(client, model, prompt, images):
    """images: list of base64 JPEG strings. Returns parsed JSON dict (or {_error:...})."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": d}}
               for d in images]
    content.append({"type": "text", "text": prompt})
    try:
        resp = client.messages.create(
            model=model, max_tokens=1024,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        )
        if resp.stop_reason == "refusal":
            return {"_error": "refusal", "_detail": getattr(resp.stop_details, "category", None)}
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1].lstrip("json").strip()
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_error": "unparseable", "_raw": text[:300]}
    except Exception as e:
        return {"_error": type(e).__name__, "_detail": str(e)[:200]}


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
        return False
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{account}/Messages.json",
            data={"From": frm, "To": to, "Body": body[:1500]},
            auth=(user, pw), timeout=20,
        )
        if r.status_code < 300:
            log("SMS sent:", body)
            return True
        log(f"SMS FAILED (HTTP {r.status_code}): {r.text[:200]}")
    except Exception as e:
        log("SMS error:", e)
    return False


def derive_mode(r):
    """Deterministically map the individual icon booleans to a single mode label.
    Flame is reported separately (flame_on); a mode can be active with the burner
    between cycles. Priority: storage > dhw > heating > idle."""
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
            sent = send_sms(f"⚠️ Boiler monitor: {sig}. ({when})")
            state["last_alert_ts"] = now
            state["alerted_open"] = True
            state["pass_since_alert"] = False
        else:
            log(f"alert suppressed (within 24h, no pass since last): {sig}")
    else:    # OK cycle == a "pass"
        if state.get("alerted_open"):
            send_sms(f"✅ Boiler monitor: previous issue cleared, now normal. ({when})")
        state["alerted_open"] = False
        state["pass_since_alert"] = True

    state["sig"] = sig
    save_state(out, state)
    result["alert"] = {"problems": probs, "sent": sent, "throttled": bool(sig) and not sent}


def cycle(client, model, out):
    os.makedirs(out, exist_ok=True)
    result = {"timestamp": datetime.now(timezone.utc).isoformat(),
              "local": datetime.now().isoformat(timespec="seconds")}

    # --- Boiler ---
    try:
        p = cap.grab_once(out, cap.CAMERAS["boiler"], name="boiler", opts={"frames": 8, "sharpen": True, "quality": 95})
        imgs = [b64_image(p), b64_image(p, crop=BOILER_DISPLAY_CROP, zoom=3)]
        reading = analyze(client, model, BOILER_PROMPT, imgs)
        if isinstance(reading, dict) and not reading.get("_error"):
            reading["mode"] = derive_mode(reading)
        result["boiler"] = {"image": os.path.basename(p), "reading": reading}
        log("boiler:", json.dumps(reading))
    except Exception as e:
        result["boiler"] = {"_error": str(e)}
        log("boiler capture failed:", e)

    # --- Zone controller (full frame + zoomed LED crop) ---
    try:
        p = cap.grab_once(out, cap.CAMERAS["zone"], name="zone", opts={"frames": 8, "sharpen": False, "quality": 95})
        imgs = [b64_image(p), b64_image(p, crop=ZONE_LED_CROP, zoom=4)]
        result["zone"] = {"image": os.path.basename(p), "reading": analyze(client, model, ZONE_PROMPT, imgs)}
        log("zone:  ", json.dumps(result["zone"]["reading"]))
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
            cycle(client, a.model, a.out)
        except Exception:
            log("cycle error:\n" + traceback.format_exc())
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
