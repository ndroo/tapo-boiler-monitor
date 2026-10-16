# Tapo Boiler Monitor

Hands-off monitoring every 30 minutes of a boiler + zone-controller via two Tapo C100 cameras —
running entirely in **GitHub Actions** (no laptop, no site hardware, no local network).

Every 30 minutes it:
1. Captures a fresh 1080p still from each camera (pure cloud: TP-Link login → MQTT relay
   → MPEG-TS → ffmpeg; reconstructed from the Tapo app, no emulator).
2. Reads the boiler LCD with Claude vision (structured JSON: temperature, every icon from
   the manual's Fig. 51 legend, **error codes**), and reads the zone panel's LEDs
   **deterministically from pixels** (white-core fraction at each LED; Claude is only asked
   when a LED is ambiguous or the POWER LED looks dark).
3. **Texts you (Twilio SMS)** if the boiler shows any error code or its screen is
   unreadable — once per state change, plus an "all clear" when it recovers.
4. Commits the latest images + `readings.jsonl` back to the repo, and a small dashboard
   (`index.html`) shows the live state.

## ecobee thermostats (every 15 minutes, free)

`ecobee_monitor.py` logs every thermostat on the account (temperature vs. heat set-point,
calling-for-heat, equipment, comfort setting / hold, humidity, remote sensors) to
`stills/ecobee.jsonl`; `.github/workflows/ecobee.yml` runs it at :03/:18/:33/:48. It signs
in like the ecobee web app (Auth0 + PKCE via `python-ecobee-api`), so no developer API key
is needed. One-time setup, in a terminal:

```bash
pip install -r requirements.txt
python3 ecobee_monitor.py login --publish   # email, password (not stored), MFA code;
                                            # writes ecobee_token.enc (encrypted), sets the
                                            # ECOBEE_TOKEN_KEY GitHub secret, commits + pushes
```

If ecobee rotates the refresh token, the job re-encrypts and commits `ecobee_token.enc`.
A password change on the account revokes the token; re-run the login command.

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
| `ECOBEE_TOKEN_KEY` | Fernet key that decrypts `ecobee_token.enc` (created by `ecobee_monitor.py login`) |

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

- Cron runs every 30 minutes at :07 and :37 (`.github/workflows/monitor.yml`). GitHub Actions compute is free at
  this volume (public repo).
- The only real cost is the Claude API: one boiler read per cycle, 48/day (~$0.018 on Opus 5.5,
  ~$26/month; ~$0.009 on Sonnet 5.5, ~$13/month, which scored 10/10 on the 2026-10-16 benchmark, see
  `legend.html`). Zone LEDs are read from pixels at no cost. The workflow runs Sonnet 5.5
  as the primary; a frame gets a second read on `--confirm-model` (default Opus, medium
  effort) when it shows an error code, is unreadable, or is low-confidence AND would change
  state (flame / DHW / heating icon, or a >=15F jump) versus the previous reading.

## Notes

- Cameras are **C100** — wired, always-on, 1920×1080 (the model's max).
- Boiler is an **HTP UFT-series, P-950EH control**. Icon legend = manual LP-542 Fig. 51
  (`docs/fig51.png`); the annotated version (`docs/fig51_legend.png`) is sent to the model
  with every capture. Radiator = heating (CH) call; small box with horizontal lines (top
  row) = DHW call; crown of flames = burner firing; two side-by-side triangles = boiler +
  CH pumps, the single one below = DHW pump. See `legend.html` on the site.
- Zone panel is a **Taco SR504-EXP**; LED positions are hard-coded in `monitor.py`
  (`ZONE_LEDS`) - re-measure if the camera moves. `--zone-llm always` forces a Claude read
  every cycle, `never` disables the fallback.
- The Pages dashboard (and this repo, if public) is viewable by anyone with the URL — it
  shows mechanical-equipment images only, no personal data.
