#!/usr/bin/env bash
# loop.sh - the body of the monitor-loop workflow. Runs for LOOP_MINUTES, polling ecobee
# (and outdoor weather) every 15 minutes and the cameras every 30 minutes, committing results after each cycle.
# Safe to run locally too (needs the same env vars / .env as the two monitors).
set -u
LOOP_MINUTES="${LOOP_MINUTES:-330}"
CAMERA_MODEL="${CAMERA_MODEL:-claude-sonnet-5-5}"
ECO_EVERY=900; CAM_EVERY=1800
end=$(( $(date +%s) + LOOP_MINUTES*60 ))
last_eco=0; last_cam=0

git config user.name  "tapo-monitor-bot"
git config user.email "github-actions[bot]@users.noreply.github.com"

commit_results() {
  # Trim the logs, stage only what exists, rebase onto anything pushed meanwhile, push.
  for f in stills/readings.jsonl stills/alerts.jsonl; do
    [ -f "$f" ] && tail -n 4000 "$f" > "$f.tmp" && mv "$f.tmp" "$f"
  done
  [ -f stills/ecobee.jsonl ] && tail -n 10000 stills/ecobee.jsonl > stills/ecobee.jsonl.tmp && mv stills/ecobee.jsonl.tmp stills/ecobee.jsonl
  for f in stills/boiler_latest.jpg stills/zone_latest.jpg stills/readings.jsonl stills/readings_latest.json \
           stills/alert_state.json stills/alerts.jsonl stills/ecobee.jsonl stills/ecobee_latest.json ecobee_token.enc \
           stills/weather.jsonl stills/weather_latest.json stills/weather_geo.json; do
    [ -f "$f" ] && git add -f "$f"
  done
  if git diff --cached --quiet; then return 0; fi
  git commit -q -m "$1 $(date -u +%Y-%m-%dT%H:%MZ)"
  for attempt in 1 2 3; do
    git pull -q --rebase origin main && git push -q origin main && return 0
    echo "push attempt $attempt failed; retrying"; sleep 10
  done
  echo "WARNING: could not push results"; return 1
}

echo "loop: ${LOOP_MINUTES} min, camera model ${CAMERA_MODEL}"
while [ "$(date +%s)" -lt "$end" ]; do
  now=$(date +%s); did=""
  if [ $((now - last_eco)) -ge $ECO_EVERY ]; then
    python ecobee_monitor.py --once || echo "ecobee cycle failed (continuing)"
    python weather_monitor.py --once || echo "weather cycle failed (continuing)"
    last_eco=$now; did="ecobee+weather"
  fi
  if [ $((now - last_cam)) -ge $CAM_EVERY ]; then
    python monitor.py --once --model "$CAMERA_MODEL" || echo "camera cycle failed (continuing)"
    last_cam=$now; did="${did:+$did+}readings"
  fi
  [ -n "$did" ] && commit_results "$did"
  # sleep until the next 15-minute boundary-ish check (poll every 30 s so timing stays tight)
  sleep 30
done
echo "loop finished at $(date -u +%H:%M:%SZ)"
