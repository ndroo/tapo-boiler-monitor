# Tapo Boiler Monitor

Hourly, hands-off monitoring of a boiler + zone-controller via two Tapo C100 cameras —
running entirely in **GitHub Actions** (no laptop, no site hardware, no local network).

Each hour it:
1. Captures a fresh 1080p still from each camera (pure cloud: TP-Link login → MQTT relay
   → MPEG-TS → ffmpeg; reconstructed from the Tapo app, no emulator).
2. Runs each image through Claude vision for structured readings
   (boiler temperature / mode / flame / **error codes**; zone power + which zones are on).
3. **Texts you (Twilio SMS)** if the boiler shows any error code or its screen is
   unreadable — once per state change, plus an "all clear" when it recovers.
4. Commits the latest images + `readings.jsonl` back to the repo, and a small dashboard
   (`index.html`) shows the live state.

## Secrets (set in repo → Settings → Secrets and variables → Actions)

**No credential is ever stored in this repo.** They live only as encrypted GitHub Secrets:

| Secret | What it is |
|---|---|
| `TPLINK_EMAIL` | TP-Link / Tapo account email |
| `TPLINK_PASSWORD` | TP-Link / Tapo account password |
| `TPLINK_TERMINAL_UUID` | a stable client UUID (any value works; reuse one to keep sessions consistent) |
| `ANTHROPIC_API_KEY` | Claude API key for the vision analysis |
| `TWILIO_ACCOUNT_SID` | Twilio **Account** SID (`AC...`) — always required, used in the request URL |
| `TWILIO_AUTH_TOKEN` | Twilio auth token *(classic auth — omit if using an API key below)* |
| `TWILIO_API_KEY_SID` | Twilio API Key SID (`SK...`) *(use instead of the auth token)* |
| `TWILIO_API_KEY_SECRET` | the API Key's secret |
| `TWILIO_FROM` | your Twilio phone number, e.g. `+1512...` |
| `ALERT_TO` | the phone number to text, e.g. `+1647...` |

Authenticate with **either** `TWILIO_AUTH_TOKEN` (classic) **or** the
`TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET` pair. `TWILIO_ACCOUNT_SID` is required
either way.

The Twilio secrets are optional — without them, capture + analysis still run and alerts are
just logged instead of texted.

## Running locally

Create a `.env` (git-ignored) next to the scripts with the same keys as above
(`KEY=value` per line), then:

```bash
pip install -r requirements.txt    # needs ffmpeg on PATH too
python3 monitor.py --once          # one capture + analysis + alert cycle
python3 grab_boiler_cloud.py --camera zone   # just grab a still
```

## Schedule / cost

- Cron runs hourly (`.github/workflows/monitor.yml`). GitHub Actions compute is free at
  this volume (public repo).
- The only real cost is the Claude API (~a few $/month hourly; use a cheaper model with
  `--model claude-haiku-5-5` if desired).

## Notes

- Cameras are **C100** — wired, always-on, 1920×1080 (the model's max).
- Boiler is an **HTP FT-series, P-950EH control**: a radiator icon = heating (CH),
  a tank/"Storage" icon = domestic hot water (DHW), a flame icon = burner firing.
- Zone panel is a **Taco SR504-EXP**; LEDs are judged ON only by a bright white-hot core
  (adjacent LEDs bleed colour).
- The Pages dashboard (and this repo, if public) is viewable by anyone with the URL — it
  shows mechanical-equipment images only, no personal data.
