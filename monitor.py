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

# HTP FT-series boiler, P-950EH control. DHW is an indirect tank the control calls "Storage".
BOILER_PROMPT = """These two images are a camera view of an HTP boiler's P-950EH controller;
the second is a zoom of its LCD. Read the LCD and report what it shows.

Mode is shown by which demand icon is lit on the LCD:
- HEATING (central heat / CH): a RADIATOR icon (several short vertical bars with a small
  pipe/thermometer on top), to the LEFT of the big temperature number.
- DHW (domestic hot water): a water-TANK / CYLINDER icon (a rounded tank shape with
  horizontal coil lines inside), in the TOP ROW of the display. The control labels this
  "Storage". If this tank icon is lit, the boiler is heating domestic hot water.
- A FLAME icon (top right) means the burner is currently firing.
- Small circle-with-triangle icons (right of the number) show which pump runs (CH vs Storage).

Judge an icon as lit only if it is clearly, solidly displayed (not a faint ghost segment).
Return ONLY a JSON object:
{
  "temperature": "<the big temperature shown, with units, or null>",
  "mode": "heating | dhw | idle | unknown",
  "flame_on": true/false,
  "heat_demand_icon": true/false,
  "dhw_storage_icon": true/false,
  "error_codes": ["<any fault/error codes shown>"],
  "display_text": "<other legible text on screen, verbatim>",
  "legible": true/false
}
If neither demand icon is lit, mode is "idle". If the screen is off or unreadable, set
legible=false and mode="unknown"."""

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
        result["boiler"] = {"image": os.path.basename(p), "reading": analyze(client, model, BOILER_PROMPT, imgs)}
        log("boiler:", json.dumps(result["boiler"]["reading"]))
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
