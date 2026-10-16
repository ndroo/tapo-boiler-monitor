#!/usr/bin/env python3
"""
grab_boiler_cloud.py - emulator-free still capture of a Tapo C100 camera.

Reconstructed from the Tapo APK. No emulator, no app, no local network - it works
from any machine with internet (your laptop, a GitHub Actions runner, anywhere):
  1. V2 cloud login            -> cloud token
  2. getAppServiceUrl + /v1/auth/app -> MQTT gateway + JWT
  3. MQTT (wss, the app's handshake) publish relay/request/get -> relayUrl + relayToken
  4. TLS to the relay host, POST /relayservice with the app's stream headers; the
     relay tunnels a multipart/mixed MPEG-TS stream from the camera
  5. demux the TS, hand a keyframe window to ffmpeg -> JPEG (sharpest of N frames)

Usage:  python3 grab_boiler_cloud.py [--camera boiler|zone] [--device <id>] [--debug]

Credentials are read from the environment (never hardcoded):
  TPLINK_EMAIL, TPLINK_PASSWORD, TPLINK_TERMINAL_UUID
For local runs, put them in a git-ignored .env file next to this script.
"""
import argparse, json, hashlib, hmac, base64, uuid, socket, ssl, sys, time, os, re, subprocess, tempfile
import requests, urllib3
from urllib.parse import urlparse
import paho.mqtt.client as mqtt
from datetime import datetime
urllib3.disable_warnings()

HERE = os.path.dirname(os.path.abspath(__file__))


def load_dotenv(path=None):
    """Load KEY=VALUE lines from a .env file into os.environ (without overriding
    anything already set). Safe no-op if the file is absent (e.g. in CI, where the
    values come from GitHub Secrets instead)."""
    path = path or os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


load_dotenv()


def _require(name):
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Missing required environment variable {name}. "
                 f"Set it in the environment, in a local .env file, or (in CI) as a GitHub Secret.")
    return v


EMAIL = _require("TPLINK_EMAIL")
PASSWORD = _require("TPLINK_PASSWORD")
TERM = os.environ.get("TPLINK_TERMINAL_UUID") or str(uuid.uuid4()).upper()

# App-level constants extracted from the APK (identical in every install - not secret).
AK, SK = "4d11b6b9d5ea4d19a829adbb9714b057", "6ed7d97f3e73467f8a5bab90b577ba4c"
APP, VER = "TP-Link_Tapo_Android", "3.7.113"

# Cameras on the account (alias -> deviceId). Device IDs are opaque hardware
# identifiers - useless without the account password - so they live in code as
# defaults, and can be overridden per-alias with TAPO_<ALIAS>_DEVICE env vars.
CAMERAS = {
    "boiler": os.environ.get("TAPO_BOILER_DEVICE", "8021B85E0B01678EECC5649C225AAA28234B5C71"),
    "zone":   os.environ.get("TAPO_ZONE_DEVICE",   "8021B086176FF38DD640693F7636ECC123D3BA54"),
}

GW = "https://n-wap-gw.tplinkcloud.com"
CLIENT_ID = f"app:{APP}:{TERM}"
USERNAME = f"{APP}:v{VER}:Android:en_US"
CLIENT_BOUNDARY = b"--client-stream-boundary--"
NL = "\r\n"
DEBUG = False

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
def dbg(*a):
    if DEBUG: log("  ", *a)

# ---------------------------------------------------------------- cloud + mqtt
def v2_login():
    body = {"appType": APP, "appVersion": VER, "cloudPassword": PASSWORD, "cloudUserName": EMAIL,
            "platform": "Android 10", "refreshTokenNeeded": False, "terminalMeta": "1",
            "terminalName": "Python Tapo Client", "terminalUUID": TERM}
    b = json.dumps(body, separators=(',', ':')).encode()
    md5 = base64.b64encode(hashlib.md5(b).digest()).decode(); nonce = str(uuid.uuid4()); path = "/api/v2/account/login"
    sig = hmac.new(SK.encode(), f"{md5}\n9999999999\n{nonce}\n{path}".encode(), hashlib.sha1).hexdigest()
    h = {"Content-Type": "application/json; charset=UTF-8", "Content-MD5": md5,
         "X-Authorization": f"Timestamp=9999999999, Nonce={nonce}, AccessKey={AK}, Signature={sig}"}
    r = requests.post(GW + path, params={"appName": APP, "appVer": VER, "termID": TERM}, headers=h, data=b, verify=False, timeout=20).json()
    if r.get("error_code") != 0:
        raise RuntimeError(f"login failed: {r}")
    return r["result"]["token"]

def service_urls(token, ids):
    r = requests.post(GW, params={"token": token}, json={"method": "getAppServiceUrl", "params": {"serviceIds": ids}}, verify=False, timeout=20).json()
    return r.get("result", {}).get("serviceUrls", {})

def get_jwt(security_url, token):
    base = security_url if security_url.startswith("http") else "https://" + security_url
    r = requests.post(base.rstrip("/") + "/v1/auth/app", json={"appType": APP, "terminalUUID": TERM, "token": f"ut|{token}"}, verify=False, timeout=20)
    return r.json().get("jwt")

def request_relay_via_mqtt(token, jwt, gateway, device_id, timeout=12):
    host = urlparse(gateway if "://" in gateway else "wss://" + gateway).hostname
    got = {"ev": __import__("threading").Event(), "relay": None, "rc": None}
    reply_topic = f"$tpiot/things/{device_id}/relay/request/get/reply"

    def on_connect(c, u, f, rc, *a):
        got["rc"] = rc
        if rc == 0: got["ev"].set()
    def on_message(c, u, m):
        dbg("mqtt msg", m.topic)
        if m.topic == reply_topic:
            try: got["relay"] = json.loads(m.payload)
            except Exception: pass
    c = mqtt.Client(client_id=CLIENT_ID, clean_session=True, protocol=mqtt.MQTTv311, transport="websockets")
    c.ws_set_options(path="/mqtt")
    c.tls_set(cert_reqs=ssl.CERT_NONE); c.tls_insecure_set(True)
    c.username_pw_set(USERNAME, jwt)
    c.on_connect, c.on_message = on_connect, on_message
    c.connect(host, 443, keepalive=235); c.loop_start()
    if not got["ev"].wait(15):
        c.loop_stop(); raise RuntimeError(f"MQTT connect failed rc={got['rc']}")
    c.subscribe([(reply_topic, 1), (f"$tpiot/app/{CLIENT_ID}/refresh/reply", 1),
                 (f"$tpiot/apps/{APP}/{TERM}/api/reply", 1)])
    time.sleep(1)
    c.publish(f"$tpiot/app/{CLIENT_ID}/refresh", json.dumps({"appType": APP, "terminalUUID": TERM, "token": f"ut|{token}"}), qos=1)
    time.sleep(3)
    param = {"userToken": f"ut|{token}", "timestamp": int(time.time() * 1000),
             "requestRelayParams": {"relayParamsJson": {
                 "deviceId": device_id, "deviceType": "SMART.IPCAMERA", "cloudType": 2, "streamType": 0,
                 "preConnection": 0, "resolution": "HD", "playerId": str(uuid.uuid4()).upper(),
                 "rootCaVer": "1", "dataTimeoutCount": 0}},
             "clientToken": f"{device_id}_{int(time.time())}", "userAgent": f"{APP}/{VER}",
             "clientId": TERM, "caType": "cloud-self", "protocolVersion": 2}
    c.publish(f"$tpiot/things/{device_id}/relay/request/get", json.dumps(param), qos=1)
    deadline = time.time() + timeout
    while time.time() < deadline and not got["relay"]:
        time.sleep(0.3)
    c.loop_stop(); c.disconnect()
    if not got["relay"]:
        raise RuntimeError("no relay reply over MQTT")
    res = (got["relay"].get("relayRespJson") or got["relay"]).get("result") or {}
    if not res.get("relayUrl"):
        raise RuntimeError(f"relay error: {got['relay']}")
    return res["relayUrl"], res["relayToken"]

# ---------------------------------------------------------------- relay stream
def relay_headers(relay_url, relay_token):
    host = urlparse(relay_url).hostname
    return {
        "User-Agent": f"Client={APP} Android {VER}/1.3",
        "Keep-Relay": "3600",
        "Accept": "*/*",
        "Content-Type": f"multipart/mixed;boundary={CLIENT_BOUNDARY.decode()}",
        "Host": host,
        "Content-Length": "9223372036854775807",
        "X-token": relay_token,
        "X-Client-Model": "Python",
        "X-Client-UUID": TERM,
        "X-Redirect-Times": "0",
        "X-Pull-Mode": "2",
        "X-Version": "2.0",
        "X-Arrive-Latency": "-1",
    }

def connect_relay(relay_url, relay_token):
    u = urlparse(relay_url); host, port = u.hostname, u.port or 443
    path = u.path + ("?" + u.query if u.query else "") + "&retryTime=0"
    hdr = relay_headers(relay_url, relay_token)
    req = f"POST {path} HTTP/1.1{NL}" + "".join(f"{k}: {v}{NL}" for k, v in hdr.items()) + NL
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    sock = ctx.wrap_socket(socket.create_connection((host, port), timeout=15), server_hostname=host)
    sock.sendall(req.encode())
    dbg("relay POST sent to", host, path[:70])
    return sock

def read_stream(sock, min_ts_bytes=1_200_000, timeout=12):
    """Read the multipart/mixed response until we have enough MPEG-TS for a keyframe."""
    sock.settimeout(timeout); buf = b""; deadline = time.time() + timeout
    try:
        while time.time() < deadline and len(buf) < min_ts_bytes + 300_000:
            c = sock.recv(65536)
            if not c: break
            buf += c
    except socket.timeout:
        pass
    return buf

def demux_ts(buf):
    """Pull the raw MPEG-TS payload out of the --device-stream-boundary-- parts."""
    hdr = buf.find(b"\r\n\r\n")
    body = buf[hdr + 4:] if hdr >= 0 else buf
    ts = bytearray()
    for part in body.split(b"----device-stream-boundary--"):
        he = part.find(b"\r\n\r\n")
        if he < 0 or b"video/mp2t" not in part[:he]:
            continue
        m = re.search(rb"Content-Length:\s*(\d+)", part[:he])
        if not m:
            continue
        n = int(m.group(1))
        ts += part[he + 4:he + 4 + n]
    return bytes(ts)

def _sharpness(path):
    """Edge-energy score (higher = crisper) using only Pillow."""
    from PIL import Image, ImageFilter, ImageStat
    edges = Image.open(path).convert("L").filter(ImageFilter.FIND_EDGES)
    return sum(v * v for v in ImageStat.Stat(edges).stddev)

def ts_to_jpeg(ts, frames=8, sharpen=False, quality=95):
    """Decode `frames` frames from the TS, keep the sharpest, optionally unsharp-mask it.
    The C100 is 1080p, so this improves clarity (not pixel count). Falls back to a
    single ffmpeg-encoded frame if Pillow isn't available."""
    d = tempfile.mkdtemp(prefix="boiler_")
    tspath = os.path.join(d, "s.ts"); open(tspath, "wb").write(ts)
    try:
        try:
            from PIL import Image, ImageFilter
        except ImportError:
            out = os.path.join(d, "f.jpg")
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tspath,
                            "-frames:v", "1", "-q:v", "2", out], check=True, capture_output=True, timeout=30)
            return open(out, "rb").read()
        # extract several consecutive frames as PNG (lossless) and pick the crispest
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", tspath,
                        "-frames:v", str(frames), "-vsync", "0", os.path.join(d, "f_%03d.png")],
                       check=True, capture_output=True, timeout=45)
        pngs = sorted(p for p in (os.path.join(d, f) for f in os.listdir(d)) if p.endswith(".png"))
        if not pngs:
            raise RuntimeError("ffmpeg produced no frames")
        best = max(pngs, key=_sharpness)
        dbg(f"picked sharpest of {len(pngs)} frames: {os.path.basename(best)}")
        img = Image.open(best)
        if sharpen:
            img = img.filter(ImageFilter.UnsharpMask(radius=1.5, percent=90, threshold=2))
        jpg = os.path.join(d, "best.jpg"); img.save(jpg, "JPEG", quality=quality)
        return open(jpg, "rb").read()
    finally:
        import shutil; shutil.rmtree(d, ignore_errors=True)

def grab_once(out, device_id, name="camera", timeout=12, opts=None):
    opts = opts or {"frames": 8, "sharpen": False, "quality": 95}
    token = v2_login()
    urls = service_urls(token, ["nbu.iot-cloud-gateway.app", "nbu.iot-security.app"])
    gw = urls.get("nbu.iot-cloud-gateway.app") or "use1-app-cloudgateway.iot.i.tplinknbu.com"
    sec = urls.get("nbu.iot-security.app") or "https://use1-security.iot.i.tplinknbu.com"
    jwt = get_jwt(sec, token)
    relay_url, relay_token = request_relay_via_mqtt(token, jwt, gw, device_id)
    dbg("relay URL acquired")
    sock = connect_relay(relay_url, relay_token)
    buf = read_stream(sock, timeout=timeout)
    sock.close()

    head = buf[:400].decode("latin1", "replace")
    if "video/mp2t" not in head and "200 OK" not in head:
        raise RuntimeError(f"unexpected relay response:\n{head}")
    ts = demux_ts(buf)
    dbg(f"demuxed {len(ts)} bytes of MPEG-TS from {len(buf)} received")
    if len(ts) < 50_000:
        raise RuntimeError(f"not enough video received ({len(ts)} TS bytes)")
    jpeg = ts_to_jpeg(ts, frames=opts["frames"], sharpen=opts["sharpen"], quality=opts["quality"])

    os.makedirs(out, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = os.path.join(out, f"{name}_{stamp}.jpg")
    open(p, "wb").write(jpeg); open(os.path.join(out, f"{name}_latest.jpg"), "wb").write(jpeg)
    log(f"saved {p} ({len(jpeg)} bytes)")
    return p

def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--camera", default="boiler",
                    help=f"which camera: {', '.join(CAMERAS)} (default boiler)")
    ap.add_argument("--device", help="raw deviceId, overrides --camera")
    ap.add_argument("--out", default=os.path.join(HERE, "stills"))
    ap.add_argument("--interval", type=int, default=0)
    ap.add_argument("--frames", type=int, default=8, help="decode N frames, keep the sharpest")
    ap.add_argument("--sharpen", action="store_true", help="apply an unsharp mask for crisper dials/text")
    ap.add_argument("--quality", type=int, default=95, help="output JPEG quality (1-100)")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args(); DEBUG = a.debug
    if a.device:
        name, device_id = "camera", a.device
    else:
        if a.camera not in CAMERAS:
            sys.exit(f"unknown camera '{a.camera}'. Known: {', '.join(CAMERAS)} (or use --device <id>)")
        name, device_id = a.camera, CAMERAS[a.camera]
    opts = {"frames": a.frames, "sharpen": a.sharpen, "quality": a.quality}
    while True:
        try:
            grab_once(a.out, device_id, name=name, opts=opts)
        except Exception as e:
            log("ERROR:", e)
        if a.interval <= 0:
            break
        time.sleep(a.interval)

if __name__ == "__main__":
    main()
