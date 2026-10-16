#!/usr/bin/env python3
"""
ecobee_monitor.py - poll every ecobee thermostat on the account and log, per thermostat:
indoor temperature vs. heat set-point, whether it is calling for heat (and what equipment
is running), humidity, the active comfort setting / hold, and each remote sensor.

No developer API key is needed: it signs in the way ecobee.com's own web app does
(Auth0, authorization-code + PKCE) via the python-ecobee-api library, then uses the
normal https://api.ecobee.com/1/thermostat endpoint.

Tokens: the refresh token is kept in ecobee_token.enc, encrypted (Fernet) with the key in
the ECOBEE_TOKEN_KEY env var / GitHub Secret, so the file can live in the repo and the
hourly job can rotate it without a personal access token.

  python3 ecobee_monitor.py login          # one-time, interactive: email, password, MFA code
  python3 ecobee_monitor.py --once         # what the GitHub Action runs every 15 minutes
  python3 ecobee_monitor.py --once --show  # also pretty-print the readings
"""
import argparse, base64, getpass, json, os, subprocess, sys, time
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from pyecobee import (Ecobee, EcobeeAuthFailedError, EcobeeAuthMfaRequiredError,
                      EcobeeAuthUnknownError, InvalidTokenError)
from pyecobee.const import (ECOBEE_ACCESS_TOKEN, ECOBEE_API_KEY, ECOBEE_AUTH0_TOKEN,
                            ECOBEE_AUTHORIZATION_CODE, ECOBEE_OPTIONS_NOTIFICATIONS,
                            ECOBEE_PASSWORD, ECOBEE_REFRESH_TOKEN, ECOBEE_USERNAME)

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(HERE, "ecobee_token.enc")
OUT_DEFAULT = os.path.join(HERE, "stills")

# equipmentStatus entries that mean "heat is being produced for this zone"
HEAT_EQUIPMENT = {"heatPump", "heatPump2", "heatPump3", "auxHeat1", "auxHeat2", "auxHeat3",
                  "compHeat", "auxHotWater"}


def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_dotenv(path=os.path.join(HERE, ".env")):
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- token file
def _fernet():
    key = os.environ.get("ECOBEE_TOKEN_KEY")
    if not key:
        sys.exit("ECOBEE_TOKEN_KEY is not set (env, .env, or GitHub Secret).")
    return Fernet(key.encode())


def read_tokens():
    if not os.path.exists(TOKEN_FILE):
        sys.exit(f"{TOKEN_FILE} not found - run `python3 ecobee_monitor.py login` first.")
    return json.loads(_fernet().decrypt(open(TOKEN_FILE, "rb").read()).decode())


def write_tokens(d):
    open(TOKEN_FILE, "wb").write(_fernet().encrypt(json.dumps(d).encode()))


def blank_config(**over):
    cfg = {ECOBEE_API_KEY: None, ECOBEE_ACCESS_TOKEN: None, ECOBEE_REFRESH_TOKEN: None,
           ECOBEE_USERNAME: None, ECOBEE_PASSWORD: None, ECOBEE_AUTH0_TOKEN: None,
           ECOBEE_AUTHORIZATION_CODE: None, ECOBEE_OPTIONS_NOTIFICATIONS: "False"}
    cfg.update(over); return cfg


# ---------------------------------------------------------------- login (interactive)
def cmd_login(a):
    load_dotenv()
    key = os.environ.get("ECOBEE_TOKEN_KEY")
    if not key:
        key = Fernet.generate_key().decode(); os.environ["ECOBEE_TOKEN_KEY"] = key
        with open(os.path.join(HERE, ".env"), "a") as f:
            f.write(f"\nECOBEE_TOKEN_KEY={key}\n")
        print("Generated a new ECOBEE_TOKEN_KEY and appended it to .env (git-ignored).")
    email = a.email or input("ecobee.com email: ").strip()
    password = getpass.getpass("ecobee.com password (not stored): ")
    eco = Ecobee(config=blank_config(**{ECOBEE_USERNAME: email, ECOBEE_PASSWORD: password}))
    try:
        eco.request_tokens_web()
    except EcobeeAuthMfaRequiredError as e:
        code = input(f"MFA code ({e.challenge.mfa_type}; authenticator app or SMS): ").strip()
        try:
            eco.submit_mfa_code(e.challenge, code)
        except EcobeeAuthFailedError as err:
            sys.exit(f"MFA rejected: {err}")
    except EcobeeAuthFailedError as err:
        sys.exit(f"Login failed: {err}")
    except EcobeeAuthUnknownError as err:
        sys.exit(f"Login error: {err}")
    if not eco.refresh_token:
        sys.exit("Login succeeded but no refresh token was returned; cannot run unattended.")
    write_tokens({"refresh_token": eco.refresh_token, "access_token": eco.access_token,
                  "obtained": datetime.now(timezone.utc).isoformat(), "email": email})
    print(f"Tokens saved to {TOKEN_FILE} (encrypted). Password was not stored.")
    # prove it works + show what's on the account
    eco2 = Ecobee(config=blank_config(**{ECOBEE_REFRESH_TOKEN: eco.refresh_token, ECOBEE_ACCESS_TOKEN: eco.access_token}))
    eco2.get_thermostats()
    for t in eco2.thermostats or []:
        loc = t.get("location", {}) or {}
        print(f"  - {t.get('name')!r}  id={t.get('identifier')}  model={t.get('modelNumber')}  "
              f"{loc.get('streetAddress','')} {loc.get('city','')}  connected={t.get('runtime',{}).get('connected')}")
    if a.publish:
        print("\nPublishing: setting GitHub secret ECOBEE_TOKEN_KEY and committing ecobee_token.enc ...")
        subprocess.run(["gh", "secret", "set", "ECOBEE_TOKEN_KEY", "--body", key], check=True, cwd=HERE)
        subprocess.run(["git", "add", "ecobee_token.enc"], check=True, cwd=HERE)
        subprocess.run(["git", "commit", "-q", "-m", "Add encrypted ecobee refresh token"], check=True, cwd=HERE)
        subprocess.run(["git", "pull", "-q", "--rebase", "origin", "main"], check=True, cwd=HERE)
        subprocess.run(["git", "push", "-q", "origin", "main"], check=True, cwd=HERE)
        print("Done. The ecobee workflow will run on its next 15-minute slot.")
    else:
        print("\nNext: re-run with --publish, or manually:\n"
              f"  gh secret set ECOBEE_TOKEN_KEY --body '<the key in .env>'\n"
              "  git add ecobee_token.enc && git commit -m 'Add ecobee token' && git push")


# ---------------------------------------------------------------- polling
def tenths(v):
    try: return round(int(v) / 10.0, 1)
    except (TypeError, ValueError): return None


def summarize(t):
    """Flatten one thermostat object from the API into the record we log."""
    rt = t.get("runtime", {}) or {}; st = t.get("settings", {}) or {}; loc = t.get("location", {}) or {}
    eq = [e for e in (t.get("equipmentStatus") or "").split(",") if e]
    events = [e for e in (t.get("events") or []) if e.get("running")]
    prog = t.get("program", {}) or {}
    climates = {c.get("climateRef"): c.get("name") for c in prog.get("climates", [])}
    hold = next((e for e in events if e.get("type") in ("hold", "vacation", "quickSave", "autoAway", "autoHome", "demandResponse")), None)
    sensors = []
    for s in t.get("remoteSensors", []) or []:
        cap = {c.get("type"): c.get("value") for c in s.get("capability", [])}
        sensors.append({"name": s.get("name"), "type": s.get("type"),
                        "temp": tenths(cap.get("temperature")) if cap.get("temperature") not in (None, "unknown") else None,
                        "occupied": (cap.get("occupancy") == "true") if "occupancy" in cap else None,
                        "humidity": int(cap["humidity"]) if cap.get("humidity", "unknown") not in ("unknown", None) else None})
    return {
        "id": t.get("identifier"), "name": t.get("name"), "model": t.get("modelNumber"),
        "location": {k: loc.get(k) for k in ("name", "streetAddress", "city")},
        "connected": rt.get("connected"), "last_status": rt.get("lastStatusModified"),
        "hvac_mode": st.get("hvacMode"),
        "temp": tenths(rt.get("actualTemperature")), "humidity": rt.get("actualHumidity"),
        "set_heat": tenths(rt.get("desiredHeat")), "set_cool": tenths(rt.get("desiredCool")),
        "equipment": eq,
        "heating": any(e in HEAT_EQUIPMENT for e in eq),
        "fan": "fan" in eq,
        "climate": climates.get(prog.get("currentClimateRef"), prog.get("currentClimateRef")),
        "hold": ({"type": hold.get("type"), "name": hold.get("name"), "heat": tenths(hold.get("heatHoldTemp")),
                  "until": f"{hold.get('endDate')} {hold.get('endTime')}" if hold.get("endDate") else None}
                 if hold else None),
        "sensors": sensors,
    }


def poll(out):
    load_dotenv()
    if not os.environ.get("ECOBEE_TOKEN_KEY") or not os.path.exists(TOKEN_FILE):
        log("ecobee not configured yet (no ECOBEE_TOKEN_KEY secret and/or no ecobee_token.enc) - "
            "run `python3 ecobee_monitor.py login --publish` once. Nothing to do.")
        return None
    tok = read_tokens()
    eco = Ecobee(config=blank_config(**{ECOBEE_REFRESH_TOKEN: tok["refresh_token"], ECOBEE_ACCESS_TOKEN: tok.get("access_token")}))
    try:
        eco.refresh_tokens()
    except InvalidTokenError:
        sys.exit("ecobee refresh token rejected (password changed or access revoked). "
                 "Re-run `python3 ecobee_monitor.py login --publish`.")
    if eco.refresh_token != tok["refresh_token"]:
        tok["refresh_token"] = eco.refresh_token; tok["rotated"] = datetime.now(timezone.utc).isoformat()
        write_tokens(tok); log("refresh token rotated; ecobee_token.enc updated")
    if not eco.get_thermostats():
        sys.exit("get_thermostats failed")
    rec = {"timestamp": datetime.now(timezone.utc).isoformat(),
           "local": datetime.now().isoformat(timespec="seconds"),
           "thermostats": sorted((summarize(t) for t in eco.thermostats), key=lambda x: (x["name"] or ""))}
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "ecobee.jsonl"), "a") as f: f.write(json.dumps(rec) + "\n")
    with open(os.path.join(out, "ecobee_latest.json"), "w") as f: json.dump(rec, f, indent=2)
    for t in rec["thermostats"]:
        log(f"{t['name']!r}: {t['temp']}°F / set {t['set_heat']}°F  {'HEATING' if t['heating'] else 'idle'}  "
            f"eq={','.join(t['equipment']) or '-'}  mode={t['hvac_mode']}  climate={t['climate']}"
            f"{'  HOLD '+str(t['hold'].get('name') or t['hold'].get('type')) if t['hold'] else ''}")
    return rec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    lg = sub.add_parser("login", help="interactive one-time sign-in"); lg.add_argument("--email"); lg.add_argument("--publish", action="store_true",
                                                                                             help="also set the GitHub secret and commit/push the token file")
    ap.add_argument("--once", action="store_true"); ap.add_argument("--show", action="store_true")
    ap.add_argument("--out", default=OUT_DEFAULT); ap.add_argument("--interval", type=int, default=900)
    a = ap.parse_args()
    if a.cmd == "login":
        return cmd_login(a)
    while True:
        rec = poll(a.out)
        if a.show and rec: print(json.dumps(rec, indent=2))
        if a.once: break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
