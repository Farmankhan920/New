"""
Stream Deck v2: restream a YouTube video/live or a Google Drive video to one or many RTMP(S)
destinations (YouTube, Facebook, Twitch, Kick, Rooter, custom) with FFmpeg.
Backend + UI live in this one file.

How it avoids cuts: ONE source process feeds a relay, and every platform has its own
small FFmpeg that only forwards from the relay. If the source link hiccups, only the
source restarts (timestamps stay continuous) and your platforms stay connected.

Two modes:
  Direct copy  (default)  -c copy, zero CPU, no overlays.
  Screen mode  (toggle)   re-encodes ONCE for all platforms and draws your Screen-tab
                          elements (text, boxes, images) on top, live.

Env vars:
  APP_PASSWORD  protect the page with a login (strongly recommended)
  APP_USER      login username (default: admin)
  YT_COOKIES    optional: contents of a cookies file (or paste it in Settings)
  YT_PROXY      optional: proxy URL used only to reach YouTube
  KEEPALIVE_URL optional: public URL of this app (auto-detected on Render/Koyeb)
"""
import io
import json
import os
import queue
import re
import secrets
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
import zlib
from collections import deque
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, jsonify, request

app = Flask(__name__)

APP_USER = os.environ.get("APP_USER", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD")
ENV_COOKIES = os.environ.get("YT_COOKIES", "")

YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")
KEEP_RE = re.compile(r"error|fail|refus|timed? ?out|invalid|unable|denied|reset|broken|not found|"
                     r"server|reconnect|warning|exit|^Input #0|^Output #0", re.I)
SRC_ERR_RE = re.compile(r"HTTP error 4|Server returned 4|Forbidden|Not Found|\b40[34]\b|\b410\b", re.I)
ERR_RE = re.compile(r"error|fail|refus|timed? ?out|reset|broken|denied", re.I)
NOISE_RE = re.compile(r"decode_slice_header|non-existing PPS|no frame!|co located POCs|mmco|"
                      r"reference picture missing|Invalid NAL|corrupt|concealing|error while decoding|"
                      r"left block unavailable|top block unavailable", re.I)
STALL_SECONDS = 60   # no data for this long -> restart that ffmpeg
MAX_FAILS = 30       # consecutive failed attempts before giving up
OUT_FAIL_LIMIT = 8   # consecutive "destination refused us" failures before giving up
MAX_DESTINATIONS = 8
TS_PKT = 188         # MPEG-TS packet size (relay chunks are always whole packets)
TS_MARGIN = 3.0      # seconds added to timestamps after a source restart (keeps them rising)
OUT_Q = {"480": (854, 480, 1400), "720": (1280, 720, 3500), "1080": (1920, 1080, 6000)}

lock = threading.Lock()
state = {"sess": None, "logs": deque(maxlen=150)}

TEST_SRC = {"test": True, "urls": [], "live": True, "ua": UA,
            "title": "Test pattern (30 seconds)", "duration": 0}

OVERLAY_PATH = os.path.join(tempfile.gettempdir(), "streamdeck_overlay.png")
SNAP_CACHE = {}


def make_blank_png(w=1280, h=720):
    """Fully transparent PNG. Same size as the real overlay so ffmpeg's filter never has to reset."""
    def chunk(t, d):
        c = struct.pack(">I", len(d)) + t + d
        return c + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    row = b"\x00" + b"\x00\x00\x00\x00" * w
    raw = row * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def write_overlay(data):
    tmp = OVERLAY_PATH + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, OVERLAY_PATH)  # atomic: ffmpeg never reads a half-written file


write_overlay(make_blank_png())


class NeedCookies(ValueError):
    """YouTube blocked the server's IP and wants a logged-in session."""


# ---------------------------------------------------------------- auth
@app.before_request
def require_login():
    if not APP_PASSWORD or request.path == "/health":
        return None
    a = request.authorization
    if a and secrets.compare_digest((a.username or "").encode(), APP_USER.encode()) \
            and secrets.compare_digest((a.password or "").encode(), APP_PASSWORD.encode()):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Stream Deck"'})


# ---------------------------------------------------------------- source resolving
def detect_source(url):
    host = (urlparse(url).hostname or "").lower()
    if host in YT_HOSTS:
        return "youtube"
    if host in DRIVE_HOSTS:
        return "drive"
    raise ValueError("Paste a YouTube or Google Drive link.")


def probe_duration(url):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-user_agent", UA, "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", url],
            capture_output=True, text=True, timeout=25).stdout.strip()
        return int(float(out))
    except Exception:
        return 0


def resolve_drive(url):
    m = re.search(r"/d/([\w-]+)", url) or re.search(r"[?&]id=([\w-]+)", url)
    if not m:
        raise ValueError("Couldn't find a file ID in that Drive link.")
    direct = ("https://drive.usercontent.google.com/download"
              f"?id={m.group(1)}&export=download&confirm=t")
    return {"urls": [direct], "live": False, "ua": UA, "title": "Google Drive video",
            "duration": probe_duration(direct)}


def cookies_to_netscape(raw):
    """Accept Netscape cookies.txt or Cookie-Editor style JSON; return Netscape text."""
    raw = (raw or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    if raw[0] in "[{":
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError("Cookies look like JSON but couldn't be read. "
                             "Export again and paste the whole text.")
        if isinstance(data, dict):
            data = data.get("cookies", [])
        far = int(time.time()) + 365 * 24 * 3600
        lines = ["# Netscape HTTP Cookie File"]
        for c in data:
            try:
                domain = c["domain"]
                host_only = bool(c.get("hostOnly", not domain.startswith(".")))
                if not host_only and not domain.startswith("."):
                    domain = "." + domain
                exp = c.get("expirationDate")
                exp = int(exp) if exp and not c.get("session") else far
                lines.append("\t".join([
                    ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
                    "FALSE" if host_only else "TRUE",
                    c.get("path") or "/",
                    "TRUE" if c.get("secure") else "FALSE",
                    str(exp), c["name"], str(c.get("value", "")),
                ]))
            except (KeyError, TypeError, AttributeError):
                continue
        if len(lines) == 1:
            raise ValueError("No usable cookies found in what you pasted.")
        return "\n".join(lines)
    if not raw.startswith(("# Netscape", "# HTTP Cookie")):
        raw = "# Netscape HTTP Cookie File\n" + raw
    return raw


def resolve_youtube(url, quality, cookie_text=""):
    cap = {"best": "", "1080": "[height<=1080]", "720": "[height<=720]", "480": "[height<=480]"}.get(quality, "")
    # H.264 + AAC only, because FLV with -c copy accepts nothing else.
    fmt = (f"bv*[vcodec^=avc1]{cap}+ba[acodec^=mp4a]/b[vcodec^=avc1]{cap}"
           "/bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[vcodec^=avc1]/b")
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "format": fmt}
    if os.environ.get("YT_PROXY"):
        opts["proxy"] = os.environ["YT_PROXY"]

    cookie_path = None
    text = cookies_to_netscape(cookie_text or ENV_COOKIES or "")
    if text:
        fd, cookie_path = tempfile.mkstemp(suffix=".txt")
        with os.fdopen(fd, "w") as f:
            f.write(text + "\n")
        opts["cookiefile"] = cookie_path

    info, last_err = None, None
    try:
        # Try several YouTube player clients; some get past the bot check.
        for client in (None, "tv", "android_vr", "web_safari", "mweb"):
            o = dict(opts)
            if client:
                o["extractor_args"] = {"youtube": {"player_client": [client]}}
            try:
                with yt_dlp.YoutubeDL(o) as ydl:
                    info = ydl.extract_info(url, download=False)
                break
            except yt_dlp.utils.DownloadError as e:
                last_err = e
    finally:
        if cookie_path:
            try:
                os.remove(cookie_path)
            except OSError:
                pass

    if info is None:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(last_err)).replace("ERROR: ", "")
        if "not a bot" in msg or "Sign in" in msg:
            raise NeedCookies("YouTube blocked this server. Paste your YouTube cookies in "
                              "Settings, then press Run again.")
        raise ValueError(f"YouTube said no: {msg[:220]}")

    fmts = info.get("requested_formats") or [info]
    vcodec = (fmts[0].get("vcodec") or "")
    # Screen mode re-encodes, so any codec works there; direct copy needs H.264.
    headers = info.get("http_headers") or {}
    return {"urls": [f["url"] for f in fmts], "live": bool(info.get("is_live")),
            "ua": headers.get("User-Agent", UA), "title": info.get("title") or "YouTube video",
            "vcodec": vcodec,
            "duration": 0 if info.get("is_live") else int(info.get("duration") or 0)}


def resolve_source(req):
    if req["kind"] == "test":
        return dict(TEST_SRC)
    if req["kind"] == "drive":
        return resolve_drive(req["video_url"])
    return resolve_youtube(req["video_url"], req["quality"], req["cookies"])


# ---------------------------------------------------------------- helpers
def is_running():
    s = state["sess"]
    return bool(s and s["active"])


def log(msg, tag=None):
    state["logs"].append(f"{tag} | {msg}" if tag else msg)


def hms(sec):
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def nap(sess, seconds, d=None):
    for _ in range(int(seconds)):
        if sess["stop"] or (d and d["stop"]):
            return
        time.sleep(1)


def kill_proc(p):
    try:
        if p and p.poll() is None:
            p.terminate()
    except OSError:
        pass


def wait_proc(p, t=8):
    try:
        p.wait(timeout=t)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def d_put(d, data):
    """Hand a chunk to a destination. Never blocks: if that platform is slow, drop its oldest chunk."""
    q = d["q"]
    while True:
        try:
            q.put_nowait(data)
            return
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass


def diagnose(output):
    """Say why the destination can't be reached: DNS, TCP, TLS, or the server itself refusing."""
    p = urlparse(output)
    host, port = p.hostname, p.port
    try:
        ip = socket.gethostbyname(host)
    except OSError as e:
        return f"[check] DNS lookup for {host} failed ({e}). The stream URL looks wrong."
    try:
        sock = socket.create_connection((host, port), timeout=8)
    except OSError as e:
        return f"[check] {host} resolved ({ip}) but port {port} is unreachable ({e})."
    if p.scheme != "rtmps":
        sock.close()
        return f"[check] DNS and TCP to {host}:{port} are fine, so the server itself refused the stream."
    try:
        with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as t:
            ver = t.version()
        return (f"[check] DNS, TCP and TLS ({ver}) to {host}:{port} all work, so the platform "
                "itself is refusing the stream. Usually the key or URL is expired or wrong, or "
                "the platform isn't waiting for a stream yet.")
    except (ssl.SSLError, OSError) as e:
        return f"[check] TLS handshake with {host}:{port} failed ({e})."


def normalize_stream_url(url):
    """Make the ingest URL explicit: scheme://host:port/path (IVS hosts need :443 and /app)."""
    p = urlparse(url)
    host = p.hostname or ""
    port = p.port or (443 if p.scheme == "rtmps" else 1935)
    path = p.path.rstrip("/")
    if not path and host.endswith("live-video.net"):
        path = "/app"
    return f"{p.scheme}://{host}:{port}{path}"


# ---------------------------------------------------------------- ffmpeg command lines
def build_source_cmd(src, req, start_at, offset, mode):
    """One ffmpeg that reads the video and writes MPEG-TS to stdout (the relay)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "info"]
    if src.get("test"):  # colour bars + tone, to check a destination accepts us
        return cmd + ["-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
                      "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                      "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                      "-g", "30", "-pix_fmt", "yuv420p", "-b:v", "600k",
                      "-c:a", "aac", "-b:a", "96k", "-ar", "44100",
                      "-flush_packets", "1", "-f", "mpegts", "pipe:1"]
    live = src["live"]
    for url in src["urls"]:
        cmd += ["-user_agent", src["ua"], "-thread_queue_size", "512",
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-rw_timeout", "20000000", "-fflags", "+genpts+discardcorrupt"]
        if start_at > 0 and not live:
            cmd += ["-ss", f"{start_at:.2f}"]
        if req["loop"] and not live:
            cmd += ["-stream_loop", "-1"]
        if not live:
            cmd += ["-re"]
        cmd += ["-i", url]
    n = len(src["urls"])
    amap = ["-map", "1:a:0"] if n == 2 else ["-map", "0:a:0?"]
    if mode == "encode":
        w, h, kb = OUT_Q.get(req.get("oq"), OUT_Q["720"])
        # The overlay PNG is re-read from disk twice a second, so edits show up live.
        # NO -re here: on a 2fps input it throttled the whole pipeline to 2fps.
        cmd += ["-f", "image2", "-framerate", "2", "-loop", "1", "-i", OVERLAY_PATH]
        fc = (f"[0:v:0]scale={w}:{h}:flags=fast_bilinear:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps=25,format=yuv420p[bg];"
              f"[{n}:v:0]scale={w}:{h}:flags=fast_bilinear,format=yuva420p[ov];"
              "[bg][ov]overlay=0:0:format=yuv420:eof_action=repeat[v]")
        cmd += ["-filter_complex", fc, "-map", "[v]"] + amap
        cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-b:v", f"{kb}k", "-maxrate", f"{kb}k", "-bufsize", f"{kb * 2}k",
                "-g", "50", "-keyint_min", "50", "-sc_threshold", "0",
                "-force_key_frames", "expr:gte(t,n_forced*2)",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", "-af", "aresample=async=1"]
    else:
        cmd += ["-map", "0:v:0"] + amap + ["-c", "copy"]
    if offset > 0:
        cmd += ["-output_ts_offset", f"{offset:.2f}"]
    cmd += ["-flush_packets", "1", "-mpegts_flags", "+resend_headers", "-f", "mpegts", "pipe:1"]
    return cmd


def build_dest_cmd(output):
    """One small ffmpeg per platform: relay in on stdin, RTMP out. Never touches the video."""
    return ["ffmpeg", "-hide_banner", "-loglevel", "info", "-fflags", "+genpts",
            "-analyzeduration", "2000000", "-probesize", "2000000", "-f", "mpegts", "-i", "pipe:0",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-flvflags", "no_duration_filesize", "-f", "flv", output]


# ---------------------------------------------------------------- source side
def pump_src_logs(sess, proc, run):
    err = io.TextIOWrapper(proc.stderr, encoding="utf-8", errors="replace")
    for line in err:
        line = line.strip()
        if not line:
            continue
        line = line[:300]
        if TIME_RE.search(line):
            sess["progress"] = line
            continue
        if NOISE_RE.search(line):
            continue
        if SRC_ERR_RE.search(line):
            run["src_err"] = True
        if ERR_RE.search(line):
            run["last_err"] = line[:160]
        if KEEP_RE.search(line):
            log(line, "source")


def pump_data(sess, proc, run):
    """Read MPEG-TS from the source and fan it out, always in whole 188-byte packets."""
    fd = proc.stdout.fileno()
    rem = b""
    while True:
        try:
            chunk = os.read(fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf = rem + chunk
        n = len(buf) - len(buf) % TS_PKT
        rem = buf[n:]
        if not n:
            continue
        data = buf[:n]
        now = time.time()
        run["last"] = now
        run["beat"] = now
        if run["first"] is None:
            run["first"] = now
        sess["last_chunk"] = now
        for d in list(sess["dests"].values()):
            if d["active"]:
                d_put(d, data)


def resume_point(sess, src, req):
    if src["live"] or sess["pos"] < 5:
        return 0.0
    pos = sess["pos"] - 3  # replay a few seconds so nothing is skipped
    dur = src.get("duration") or 0
    if req["loop"]:
        return (pos % dur) if dur else 0.0
    return max(0.0, pos)


def run_source(sess):
    """Keep the single source running. A drop here never disconnects any platform."""
    req = sess["req"]
    src = sess["src"]
    fails, first, rc, reuse, runs = 0, True, None, False, 0
    try:
        while not sess["stop"]:
            if not first:
                sess["phase"] = "reconnecting"
                if not reuse:
                    try:
                        src = resolve_source(req)
                        sess["src"] = src
                    except ValueError as e:
                        fails += 1
                        log(f"[could not refresh the video link: {e}]", "source")
                        if fails > MAX_FAILS:
                            break
                        nap(sess, min(5 + 3 * fails, 30))
                        continue
            first = False
            dur = src.get("duration") or 0
            start_at = resume_point(sess, src, req)
            offset = 0.0 if runs == 0 else time.time() - sess["t0"] + TS_MARGIN
            runs += 1
            cmd = build_source_cmd(src, req, start_at, offset, sess["mode"])
            run = {"beat": time.time(), "first": None, "last": None, "src_err": False, "last_err": ""}
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
            sess["proc"] = proc
            sess["phase"] = "streaming"
            t1 = threading.Thread(target=pump_src_logs, args=(sess, proc, run), daemon=True)
            t2 = threading.Thread(target=pump_data, args=(sess, proc, run), daemon=True)
            t1.start()
            t2.start()

            while proc.poll() is None:
                if sess["stop"]:
                    break
                now = time.time()
                if run["first"] and not src["live"]:
                    sess["pos"] = start_at + (now - run["first"])
                if now - run["beat"] > STALL_SECONDS:
                    log(f"[no video data for {STALL_SECONDS}s, restarting the source]", "source")
                    proc.kill()
                    break
                time.sleep(1)
            if sess["stop"]:
                kill_proc(proc)
            wait_proc(proc)
            t1.join(timeout=3)
            t2.join(timeout=3)
            rc = proc.returncode
            if sess["stop"]:
                break

            run_len = (run["last"] - run["first"]) if run["first"] and run["last"] else 0
            if req["loop"]:
                complete = False
            elif src.get("test"):
                complete = rc == 0
            elif src["live"]:
                complete = rc == 0
                if complete:
                    # a clean exit can just be a hiccup: only stop if the live is really over
                    try:
                        fresh = resolve_source(req)
                    except ValueError:
                        fresh = None
                    if fresh is not None and fresh.get("live"):
                        complete = False
                        src = fresh
                        sess["src"] = fresh
            elif dur:
                complete = sess["pos"] >= dur - 8
            else:
                complete = rc == 0
            if complete:
                log("[finished: the video played to the end]", "source")
                break

            fails = 0 if run_len >= 20 else fails + 1
            if fails > MAX_FAILS:
                log("[source gave up after repeated failures]", "source")
                break
            wait = min(2 + 3 * fails, 30)
            sess["restarts"] += 1
            reuse = run_len >= 5 and not run["src_err"]
            why = f": {run['last_err']}" if run["last_err"] else ""
            log(f"[source dropped (code {rc}){why}; resuming in {wait}s, platforms stay connected]", "source")
            nap(sess, wait)
    except Exception as e:  # never let a supervisor die silently
        log(f"[source error: {e}]", "source")
    finally:
        sess["src_done"] = True
        sess["phase"] = ""
        if sess["user_stop"]:
            log("[stopped]")
        elif not sess["stop"]:
            for d in list(sess["dests"].values()):
                d_put(d, None)  # tell every platform the video is over


# ---------------------------------------------------------------- destination side
def pump_dest_logs(d, proc, run):
    err = io.TextIOWrapper(proc.stderr, encoding="utf-8", errors="replace")
    for line in err:
        line = line.strip()
        if not line:
            continue
        line = line.replace(d["key"], "****")[:300]
        m = TIME_RE.search(line)
        if m:
            t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
            if t > run.get("last_t", -1.0):
                run["last_t"], run["beat"] = t, time.time()
            d["progress"] = line
        else:
            if NOISE_RE.search(line):
                continue
            if "Error opening output" in line:
                run["out_fail"] = True
            if ERR_RE.search(line):
                run["last_err"] = line[:160]
            if KEEP_RE.search(line):
                log(line, d["name"])


def dest_writer(d, proc):
    q = d["q"]
    try:
        while proc.poll() is None:
            try:
                data = q.get(timeout=1)
            except queue.Empty:
                continue
            if data is None:
                d["eof"] = True
                break
            proc.stdin.write(data)
            proc.stdin.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def dest_done(sess, d):
    d["active"] = False
    d["phase"] = ""
    d["progress"] = ""
    if not any(x["active"] for x in sess["dests"].values()):
        sess["active"] = False
        sess["stop"] = True
        kill_proc(sess.get("proc"))


def run_dest(sess, d):
    """Keep one platform connected. Only this platform restarts when its connection drops."""
    tag = d["name"]
    fails, out_fails, first, rc = 0, 0, True, None
    try:
        while not sess["stop"] and not d["stop"]:
            if not first:
                d["phase"] = "reconnecting"
                while not d["eof"]:  # drop stale video so we rejoin at the live edge
                    try:
                        if d["q"].get_nowait() is None:
                            d["eof"] = True
                    except queue.Empty:
                        break
            first = False
            run = {"beat": time.time(), "started": time.time(), "out_fail": False,
                   "last_err": "", "last_t": -1.0}
            proc = subprocess.Popen(build_dest_cmd(d["output"]), stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    start_new_session=True)
            d["proc"] = proc
            d["phase"] = "streaming"
            rd = threading.Thread(target=pump_dest_logs, args=(d, proc, run), daemon=True)
            wr = threading.Thread(target=dest_writer, args=(d, proc), daemon=True)
            rd.start()
            wr.start()

            while proc.poll() is None:
                if sess["stop"] or d["stop"]:
                    break
                now = time.time()
                if now - sess["last_chunk"] < 10 and now - run["beat"] > STALL_SECONDS:
                    log(f"[no progress for {STALL_SECONDS}s, restarting this connection]", tag)
                    proc.kill()
                    break
                time.sleep(1)
            if sess["stop"] or d["stop"]:
                kill_proc(proc)
            wait_proc(proc)
            rd.join(timeout=3)
            wr.join(timeout=3)
            rc = proc.returncode
            if sess["stop"] or d["stop"]:
                break
            if d["eof"]:
                log("[finished]", tag)
                break

            if run["out_fail"]:
                out_fails += 1
                if out_fails in (1, 4):
                    log(diagnose(d["output"]), tag)
                if out_fails >= OUT_FAIL_LIMIT:
                    log("[this platform keeps refusing the connection, so I stopped it. Get a "
                        "fresh URL and key from the platform, wait until it is ready, then "
                        "press Run again]", tag)
                    break
            else:
                out_fails = 0

            fails = 0 if time.time() - run["started"] >= 20 else fails + 1
            if fails > MAX_FAILS:
                log("[giving up after repeated failures]", tag)
                break
            wait = min(2 + 3 * fails, 30)
            d["restarts"] += 1
            why = f": {run['last_err']}" if run["last_err"] else ""
            log(f"[connection dropped (code {rc}){why}; reconnecting in {wait}s]", tag)
            nap(sess, wait, d)
    except Exception as e:
        log(f"[error: {e}]", tag)
    finally:
        d["exit_code"] = rc
        dest_done(sess, d)


def keepalive():
    """Free hosts sleep without inbound traffic; ping our own public URL while streaming."""
    base = os.environ.get("KEEPALIVE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or ""
    if not base and os.environ.get("KOYEB_PUBLIC_DOMAIN"):
        base = "https://" + os.environ["KOYEB_PUBLIC_DOMAIN"]
    if not base:
        return
    while True:
        time.sleep(240)
        if is_running():
            try:
                urllib.request.urlopen(base.rstrip("/") + "/health", timeout=15).read()
            except Exception:
                pass


threading.Thread(target=keepalive, daemon=True).start()


# ---------------------------------------------------------------- routes
@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/health")
def health():
    return "ok"


@app.post("/start")
def start():
    d = request.get_json(silent=True) or {}
    video_url = (d.get("video_url") or "").strip()
    quality = d.get("quality") or "1080"
    loop = bool(d.get("loop"))
    test = bool(d.get("test"))
    overlay = bool(d.get("overlay"))
    oq = str(d.get("oq") or "720")
    if oq not in OUT_Q:
        oq = "720"
    if overlay and not test and quality != oq:
        try:
            if quality == "best" or int(quality) > int(oq):
                quality = oq  # decoding 1080p to output 480p only wastes CPU
        except ValueError:
            pass
    cookies = d.get("cookies") or ""
    raw = d.get("destinations") or []

    if not raw:
        return jsonify(error="Select at least one platform."), 400
    if not (video_url or test):
        return jsonify(error="Paste a YouTube or Google Drive link first."), 400

    dests, seen = [], set()
    for x in raw[:MAX_DESTINATIONS]:
        if not isinstance(x, dict):
            continue
        name = str(x.get("name") or "Platform").strip()[:40]
        url = str(x.get("url") or "").strip().rstrip("/")
        key = str(x.get("key") or "").strip()
        did = str(x.get("id") or secrets.token_hex(4))[:40]
        if did in seen:
            continue
        seen.add(did)
        if not url.startswith(("rtmp://", "rtmps://")) or not urlparse(url).hostname or not key:
            return jsonify(error=f"{name}: needs an rtmp:// or rtmps:// URL and a stream key."), 400
        dests.append({"id": did, "name": name, "key": key,
                      "output": f"{normalize_stream_url(url)}/{key}"})
    if not dests:
        return jsonify(error="Select at least one platform."), 400
    if is_running():
        return jsonify(error="A stream is already running. Stop it first."), 409

    try:
        if test:
            kind, src = "test", dict(TEST_SRC)
        else:
            kind = detect_source(video_url)
            src = (resolve_drive(video_url) if kind == "drive"
                   else resolve_youtube(video_url, quality, cookies))
    except NeedCookies as e:
        return jsonify(error=str(e), need_cookies=True), 400
    except ValueError as e:
        return jsonify(error=str(e)), 400

    mode = "test" if test else ("encode" if overlay else "copy")
    if mode == "copy":
        vc = src.get("vcodec") or ""
        if vc not in ("", "none") and not vc.startswith(("avc1", "h264")):
            return jsonify(error=f"This video only has {vc.split('.')[0]} video. Direct copy needs "
                                 "H.264: turn on Screen mode (it re-encodes) or pick a lower quality."), 400

    req = {"kind": kind, "video_url": video_url, "quality": quality, "cookies": cookies,
           "loop": loop, "oq": oq}
    with lock:
        if is_running():
            return jsonify(error="A stream is already running. Stop it first."), 409
        now = time.time()
        sess = {"stop": False, "user_stop": False, "active": True, "started": now, "t0": now,
                "title": src["title"], "source": kind, "duration": src.get("duration") or 0,
                "src": src, "req": req, "mode": mode, "pos": 0.0, "phase": "streaming",
                "restarts": 0, "last_chunk": now, "proc": None, "progress": "",
                "src_done": False, "dests": {}}
        state["sess"] = sess
        state["logs"].clear()
        for x in dests:
            sess["dests"][x["id"]] = {
                "id": x["id"], "name": x["name"], "key": x["key"], "output": x["output"],
                "active": True, "stop": False, "eof": False, "proc": None,
                "q": queue.Queue(maxsize=300), "phase": "streaming", "restarts": 0,
                "progress": "", "exit_code": None}
        for dd in sess["dests"].values():
            threading.Thread(target=run_dest, args=(sess, dd), daemon=True).start()
        threading.Thread(target=run_source, args=(sess,), daemon=True).start()
    return jsonify(ok=True, title=src["title"], count=len(dests), mode=mode)


@app.post("/stop")
def stop():
    s = state["sess"]
    if s and s["active"]:
        s["user_stop"] = True
        s["stop"] = True
        dl = list(s["dests"].values())
        for dd in dl:
            dd["stop"] = True
        procs = [s.get("proc")] + [dd.get("proc") for dd in dl]
        for p in procs:
            kill_proc(p)
        for p in procs:
            if p:
                wait_proc(p)
        s["active"] = False
    return jsonify(ok=True)


@app.post("/overlay")
def overlay():
    data = request.get_data(cache=False)
    if not data.startswith(b"\x89PNG") or len(data) > 8_000_000:
        return jsonify(error="That overlay image wasn't valid."), 400
    write_overlay(data)
    return jsonify(ok=True)


@app.post("/snapshot")
def snapshot():
    """One frame of the source (no overlays), so you can place elements on the real picture."""
    d = request.get_json(silent=True) or {}
    s = state["sess"]
    src = s["src"] if (s and s["active"] and not s["src"].get("test")) else None
    if src is None:
        video_url = (d.get("video_url") or "").strip()
        if not video_url:
            return jsonify(error="Paste a YouTube or Drive link on Home first."), 400
        hit = SNAP_CACHE.get(video_url)
        if hit and time.time() - hit[0] < 600:
            src = hit[1]
        else:
            try:
                kind = detect_source(video_url)
                src = (resolve_drive(video_url) if kind == "drive"
                       else resolve_youtube(video_url, "480", d.get("cookies") or ""))
            except NeedCookies as e:
                return jsonify(error=str(e), need_cookies=True), 400
            except ValueError as e:
                return jsonify(error=str(e)), 400
            SNAP_CACHE[video_url] = (time.time(), src)

    last = "no output"
    for nokey, tmo in ((True, 18), (False, 15)):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-user_agent", src["ua"],
               "-rw_timeout", "15000000"]
        if not src["live"] and (src.get("duration") or 0) > 6:
            cmd += ["-ss", "2"]
        if nokey:
            cmd += ["-skip_frame", "nokey"]
        cmd += ["-i", src["urls"][0], "-frames:v", "1", "-an",
                "-vf", "scale=1280:720:force_original_aspect_ratio=decrease",
                "-q:v", "3", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        try:
            out = subprocess.run(cmd, capture_output=True, timeout=tmo)
        except subprocess.TimeoutExpired:
            last = "timed out (server busy)"
            continue
        except Exception as e:
            return jsonify(error=f"Frame grab failed: {e}"), 500
        if out.returncode == 0 and out.stdout:
            return Response(out.stdout, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})
        last = (out.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["no output"])[0][:140]
    SNAP_CACHE.clear()  # link may have expired, resolve again next time
    return jsonify(error="Couldn't grab a frame: " + last), 502


@app.get("/status")
def status():
    s = state["sess"]
    running = bool(s and s["active"])
    dl = list(s["dests"].values()) if s else []
    logs = list(state["logs"])
    if running:
        if s["progress"]:
            logs.append(f"source | {s['progress']}")
        logs += [f"{x['name']} | {x['progress']}" for x in dl if x["active"] and x["progress"]]
    pos = int(s["pos"]) if s else 0
    if s and s["req"]["loop"] and s["duration"]:
        pos %= s["duration"]
    return jsonify(
        running=running,
        uptime=int(time.time() - s["started"]) if running else 0,
        title=s["title"] if s else "", source=s["source"] if s else "",
        duration=s["duration"] if s else 0, pos=pos,
        mode=s["mode"] if s else "", src_phase=s["phase"] if running else "",
        restarts=s["restarts"] if s else 0,
        dests=[{"id": x["id"], "name": x["name"], "active": x["active"], "phase": x["phase"],
                "restarts": x["restarts"]} for x in dl],
        logs=logs)


# ---------------------------------------------------------------- the whole UI
INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Stream Deck</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #0d0820; --card: rgba(255,255,255,.065); --stroke: rgba(255,255,255,.13);
    --text: #f5f2ff; --muted: #a59dc8; --hot: #ff3366; --amber: #ffab2e; --cyan: #36e2ff; --ok: #3ee89a;
    --r-xl: 32px; --r-lg: 22px; --r-md: 16px;
  }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  [hidden] { display: none !important; }
  html { background: var(--bg); }
  body {
    margin: 0; min-height: 100vh; color: var(--text);
    font: 16px/1.5 "Space Grotesk", system-ui, sans-serif;
    background:
      radial-gradient(60rem 36rem at 8% -10%, rgba(255,51,102,.28), transparent 60%),
      radial-gradient(50rem 34rem at 100% 0%, rgba(54,226,255,.18), transparent 60%),
      radial-gradient(46rem 30rem at 50% 115%, rgba(124,77,255,.30), transparent 60%),
      var(--bg);
    background-attachment: fixed;
    padding: calc(24px + env(safe-area-inset-top)) 16px calc(130px + env(safe-area-inset-bottom));
  }
  .wrap { max-width: 920px; margin: 0 auto; display: grid; gap: 18px; }
  .view { display: none; gap: 18px; }
  .view.active { display: grid; }
  .card {
    background: var(--card); border: 1px solid var(--stroke); border-radius: var(--r-xl);
    padding: 24px; backdrop-filter: blur(18px); -webkit-backdrop-filter: blur(18px);
    box-shadow: 0 24px 60px -24px rgba(0,0,0,.65), inset 0 1px 0 rgba(255,255,255,.08);
  }
  h1, h2, p { margin: 0; }
  h2 { font-size: 1.05rem; font-weight: 700; margin-bottom: 4px; }
  .sub { color: var(--muted); font-size: .9rem; margin-bottom: 18px; }

  .top { display: flex; align-items: center; gap: 12px; padding: 0 6px; }
  .logo { width: 44px; height: 44px; border-radius: 15px; display: grid; place-items: center;
    background: linear-gradient(135deg, var(--hot), var(--amber)); box-shadow: 0 10px 28px -8px var(--hot); }
  .top h1 { font-size: 1.4rem; font-weight: 700; letter-spacing: -.02em; }
  .top span { color: var(--muted); font-size: .85rem; display: block; margin-top: -2px; }

  .hero { display: grid; grid-template-columns: auto 1fr; gap: 24px; align-items: center; }
  .ring { width: 150px; height: 150px; position: relative; display: grid; place-items: center; }
  .ring::before {
    content: ""; position: absolute; inset: 0; border-radius: 50%; padding: 6px;
    background: conic-gradient(from 0deg, var(--hot), var(--amber), var(--cyan), var(--hot));
    -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude; opacity: .22; transition: opacity .4s;
  }
  .live .ring::before { opacity: 1; animation: spin 3.2s linear infinite; }
  .live .ring { filter: drop-shadow(0 0 22px rgba(255,51,102,.55)); }
  @keyframes spin { to { transform: rotate(360deg); } }
  .ring-in { text-align: center; }
  .state { font-weight: 700; font-size: 1.02rem; letter-spacing: .02em; color: var(--muted); }
  .live .state { color: var(--hot); }
  .timer { font: 400 1.2rem "JetBrains Mono", ui-monospace, monospace; margin-top: 2px; }
  .now { min-width: 0; }
  .now .label { color: var(--muted); font-size: .85rem; }
  .now .title { font-size: 1.35rem; font-weight: 700; line-height: 1.25; letter-spacing: -.01em;
    margin: 4px 0 12px; overflow-wrap: anywhere; }
  .pill { display: inline-flex; align-items: center; gap: 8px; padding: 6px 14px; border-radius: 999px;
    background: rgba(255,255,255,.08); border: 1px solid var(--stroke); font-size: .85rem; color: var(--muted); }
  .pill i { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
  .live .pill i { background: var(--ok); box-shadow: 0 0 10px var(--ok); }

  label.f { display: block; font-size: .85rem; font-weight: 500; color: var(--muted); margin: 0 0 8px 4px; }
  .field { position: relative; margin-bottom: 18px; }
  input[type=text], input[type=url], input[type=password], select {
    width: 100%; font: inherit; color: var(--text); padding: 14px 18px; border-radius: var(--r-md);
    background: rgba(8,4,22,.55); border: 1px solid var(--stroke); transition: border-color .2s, box-shadow .2s;
  }
  select { appearance: none; -webkit-appearance: none; cursor: pointer;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='14' height='8' fill='none' stroke='%23a59dc8' stroke-width='2'><path d='M1 1l6 6 6-6'/></svg>");
    background-repeat: no-repeat; background-position: right 18px center; padding-right: 44px; }
  select option { background: #1a1236; }
  input::placeholder, textarea::placeholder { color: rgba(165,157,200,.55); }
  input:focus-visible, select:focus-visible, textarea:focus-visible {
    outline: none; border-color: var(--cyan); box-shadow: 0 0 0 4px rgba(54,226,255,.18); }
  textarea { width: 100%; font: 12px/1.5 "JetBrains Mono", ui-monospace, monospace; color: var(--text); min-height: 120px;
    padding: 14px 18px; border-radius: var(--r-md); background: rgba(8,4,22,.55); border: 1px solid var(--stroke); resize: vertical; }
  .eye { position: absolute; right: 8px; top: 36px; width: 36px; height: 36px; border-radius: 12px; border: 0;
    background: rgba(255,255,255,.08); color: var(--muted); cursor: pointer; display: grid; place-items: center; }
  .eye:hover { color: var(--text); }
  .has-eye input { padding-right: 56px; }
  .detect { position: absolute; right: 12px; top: 41px; padding: 4px 12px; border-radius: 999px; font-size: .78rem;
    font-weight: 700; display: none; }
  .detect.yt { display: block; background: rgba(255,51,102,.18); color: #ff7a9a; }
  .detect.dr { display: block; background: rgba(54,226,255,.16); color: var(--cyan); }
  .has-detect input { padding-right: 120px; }

  .chips { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }
  .chip { padding: 8px 16px; border-radius: 999px; font: inherit; font-size: .88rem; font-weight: 500; cursor: pointer;
    color: var(--text); background: rgba(255,255,255,.07); border: 1px solid var(--stroke); transition: all .2s; }
  .chip:hover { background: rgba(255,255,255,.13); }

  .toggle { display: flex; align-items: center; justify-content: space-between; gap: 12px; cursor: pointer;
    padding: 12px 16px; border-radius: var(--r-md); background: rgba(8,4,22,.55); border: 1px solid var(--stroke); position: relative; }
  .toggle + .toggle, .toggle + .field { margin-top: 12px; }
  .toggle b { font-weight: 500; font-size: .95rem; }
  .toggle small { color: var(--muted); display: block; font-size: .8rem; }
  .toggle input { position: absolute; opacity: 0; pointer-events: none; }
  .sw { flex: none; width: 48px; height: 28px; border-radius: 999px; background: rgba(255,255,255,.15); position: relative; transition: background .2s; }
  .sw::after { content: ""; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px; border-radius: 50%; background: #fff; transition: transform .2s; }
  .toggle input:checked + .sw { background: var(--ok); }
  .toggle input:checked + .sw::after { transform: translateX(20px); }
  .toggle:focus-within { box-shadow: 0 0 0 4px rgba(54,226,255,.18); border-color: var(--cyan); }

  .mini { font: inherit; font-size: .85rem; font-weight: 500; padding: 8px 16px; border-radius: 999px; cursor: pointer;
    color: var(--text); background: rgba(255,255,255,.08); border: 1px solid var(--stroke); }
  .mini:hover:not(:disabled) { background: rgba(255,255,255,.15); }
  .mini.danger:hover:not(:disabled) { background: rgba(255,51,102,.22); border-color: var(--hot); }
  .mini:disabled { opacity: .4; cursor: not-allowed; }
  .head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }

  .plist { display: grid; gap: 12px; }
  .plat { display: flex; align-items: center; gap: 14px; width: 100%; text-align: left; font: inherit; color: var(--text);
    padding: 14px 16px; border-radius: var(--r-lg); cursor: pointer; border: 2px solid transparent;
    background: linear-gradient(#1a1337, #1a1337) padding-box, linear-gradient(rgba(255,255,255,.14), rgba(255,255,255,.14)) border-box;
    transition: transform .15s, background .2s; }
  .plat:active:not([aria-disabled=true]) { transform: scale(.985); }
  .plat[aria-checked=true] {
    background: linear-gradient(#241a47, #241a47) padding-box, linear-gradient(120deg, var(--hot), var(--amber)) border-box; }
  .plat[aria-disabled=true] { cursor: default; }
  .plat:focus-visible { outline: 3px solid var(--cyan); outline-offset: 2px; }
  .av { flex: none; width: 46px; height: 46px; border-radius: 16px; display: grid; place-items: center;
    font-weight: 700; font-size: 1.2rem; color: #1b0a12; }
  .pt { flex: 1; min-width: 0; display: block; }
  .pt b { display: block; font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pt small { display: block; color: var(--muted); font-size: .82rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pt small[data-st=live] { color: var(--ok); } .pt small[data-st=retry] { color: var(--amber); } .pt small[data-st=off] { color: var(--muted); }
  .ck { flex: none; width: 32px; height: 32px; border-radius: 50%; border: 2px solid rgba(255,255,255,.28);
    display: grid; place-items: center; transition: all .2s; }
  .ck svg { opacity: 0; transform: scale(.5); transition: all .2s; }
  .plat[aria-checked=true] .ck { background: linear-gradient(135deg, var(--hot), var(--amber)); border-color: transparent; }
  .plat[aria-checked=true] .ck svg { opacity: 1; transform: scale(1); }
  .empty { text-align: center; padding: 26px 12px; color: var(--muted); border: 2px dashed var(--stroke); border-radius: var(--r-lg); }
  .empty p { margin-bottom: 14px; }

  .srow { padding: 14px 16px; border-radius: var(--r-lg); background: rgba(8,4,22,.45); border: 1px solid var(--stroke); }
  .srow .top2 { display: flex; align-items: center; gap: 14px; }
  .srow .btns { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
  .steps { color: var(--muted); font-size: .88rem; margin: 0 0 14px; padding-left: 20px; }
  .steps li { margin-bottom: 4px; }
  .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
  #cookieState { font-size: .85rem; color: var(--muted); font-weight: 400; }
  #cookieState.on { color: var(--ok); }

  .actions { display: grid; grid-template-columns: 2fr 1fr; gap: 14px; }
  .btn { font: inherit; font-weight: 700; font-size: 1.08rem; padding: 18px 24px; border-radius: 22px; border: 0; cursor: pointer;
    display: flex; align-items: center; justify-content: center; gap: 10px; transition: transform .15s, box-shadow .2s, opacity .2s; }
  .btn:active:not(:disabled) { transform: scale(.98); }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn:focus-visible { outline: 3px solid var(--cyan); outline-offset: 3px; }
  .btn.save { width: 100%; color: #1b0a12; background: linear-gradient(120deg, var(--hot), var(--amber)); box-shadow: 0 16px 40px -12px rgba(255,51,102,.7); }
  #run { color: #1b0a12; background: linear-gradient(120deg, var(--hot), var(--amber)); box-shadow: 0 16px 40px -12px rgba(255,51,102,.7); }
  #run:hover:not(:disabled) { box-shadow: 0 20px 50px -10px rgba(255,51,102,.9); }
  #stop { color: var(--text); background: rgba(255,255,255,.08); border: 1px solid var(--stroke); }
  #stop:hover:not(:disabled) { background: rgba(255,51,102,.22); border-color: var(--hot); }

  details.card { padding: 0; }
  summary { cursor: pointer; padding: 20px 24px; font-weight: 700; list-style: none; display: flex; justify-content: space-between; align-items: center; gap: 10px; }
  summary::-webkit-details-marker { display: none; }
  summary::after { content: "+"; flex: none; width: 30px; height: 30px; display: grid; place-items: center; border-radius: 50%; background: rgba(255,255,255,.09); color: var(--muted); }
  details[open] summary::after { content: "\2212"; }
  summary:focus-visible { outline: 3px solid var(--cyan); outline-offset: -3px; border-radius: var(--r-xl); }
  .pad { padding: 0 24px 24px; }
  pre { margin: 0 14px 14px; padding: 16px 18px; border-radius: var(--r-lg); background: rgba(5,2,16,.8);
    border: 1px solid var(--stroke); color: #c5f3ff; font: 12px/1.65 "JetBrains Mono", ui-monospace, monospace;
    max-height: 260px; overflow: auto; white-space: pre-wrap; word-break: break-all; }
  .note { color: var(--muted); font-size: .82rem; text-align: center; padding: 0 12px; }

  .nav { position: fixed; z-index: 8; left: 50%; transform: translateX(-50%);
    bottom: calc(14px + env(safe-area-inset-bottom)); width: min(460px, calc(100vw - 32px));
    display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 6px; padding: 6px; border-radius: 30px;
    background: rgba(24,16,52,.78); border: 1px solid var(--stroke);
    backdrop-filter: blur(22px); -webkit-backdrop-filter: blur(22px);
    box-shadow: 0 20px 50px -12px rgba(0,0,0,.85); }
  .nav button { font: inherit; font-weight: 700; font-size: .88rem; color: var(--muted); border: 0; background: transparent;
    border-radius: 24px; padding: 12px 4px; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 6px;
    transition: all .2s; }
  .nav button[aria-current=page] { color: #1b0a12; background: linear-gradient(120deg, var(--hot), var(--amber));
    box-shadow: 0 10px 26px -10px rgba(255,51,102,.8); }
  .nav button:focus-visible { outline: 3px solid var(--cyan); outline-offset: 1px; }

  #toast { position: fixed; left: 50%; bottom: calc(96px + env(safe-area-inset-bottom)); transform: translate(-50%, 200%); z-index: 9;
    max-width: calc(100vw - 32px); padding: 14px 22px; border-radius: 18px; font-weight: 500; background: #241a47;
    border: 1px solid var(--stroke); box-shadow: 0 18px 50px -10px rgba(0,0,0,.8); transition: transform .35s cubic-bezier(.2,.9,.3,1.2); }
  #toast.show { transform: translate(-50%, 0); }
  #toast.err { border-color: var(--hot); } #toast.ok { border-color: var(--ok); }

  /* screen editor */
  .stage { border-radius: var(--r-lg); overflow: hidden; border: 1px solid var(--stroke); background: #0b0720; line-height: 0; }
  #cv { width: 100%; height: auto; display: block; touch-action: none; cursor: grab; user-select: none; -webkit-user-select: none; }
  .badge { font-size: .82rem; padding: 8px 14px; border-radius: 14px; margin-bottom: 12px; border: 1px solid var(--stroke); background: rgba(255,255,255,.06); color: var(--muted); }
  .badge.on { color: var(--ok); border-color: rgba(62,232,154,.4); }
  .badge.warn { color: var(--amber); border-color: rgba(255,171,46,.4); }
  .tools { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .pgrid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 14px; }
  .pr { margin-bottom: 14px; }
  .pgrid > div, .pr { min-width: 0; }
  input[type=range] { width: 100%; accent-color: var(--hot); }
  input[type=color] { width: 100%; height: 44px; border: 1px solid var(--stroke); border-radius: 12px; background: rgba(8,4,22,.55); padding: 4px; }
  .cb { display: flex; align-items: center; gap: 8px; font-size: .85rem; color: var(--muted); margin-top: 6px; }
  #p_text { font: 15px/1.4 "Space Grotesk", system-ui, sans-serif; min-height: 70px; }

  @media (max-width: 720px) {
    .hero { grid-template-columns: 1fr; justify-items: center; text-align: center; }
    .actions { grid-template-columns: 1fr; }
    .card { border-radius: 28px; padding: 20px; }
    .pad { padding: 0 20px 20px; }
  }
  @media (prefers-reduced-motion: reduce) { .live .ring::before { animation: none; } * { transition: none !important; } }
</style>
</head>
<body>
<div class="wrap" id="root">

  <div class="top">
    <div class="logo" aria-hidden="true">
      <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#1b0a12" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="2.2" fill="#1b0a12"/><path d="M7.2 7.2a6.8 6.8 0 0 0 0 9.6M16.8 7.2a6.8 6.8 0 0 1 0 9.6M4.2 4.2a11 11 0 0 0 0 15.6M19.8 4.2a11 11 0 0 1 0 15.6"/></svg>
    </div>
    <div><h1>Stream Deck</h1><span>One video, many platforms</span></div>
  </div>

  <!-- ================= HOME ================= -->
  <main class="view active" id="view-home">
    <section class="card hero" aria-live="polite">
      <div class="ring"><div class="ring-in"><div class="state" id="state">OFFLINE</div><div class="timer" id="timer">00:00:00</div></div></div>
      <div class="now">
        <div class="label" id="nowLabel">Nothing is streaming</div>
        <div class="title" id="nowTitle">Ready when you are</div>
        <span class="pill"><i></i><span id="pillText">Server idle</span></span>
        <div class="label" id="prog" style="margin-top:12px"></div>
      </div>
    </section>

    <section class="card">
      <h2>Source</h2>
      <p class="sub">A YouTube video or live link, or a Drive file shared with “Anyone with the link”.</p>
      <div class="field has-detect">
        <label class="f" for="video_url">Video link</label>
        <input id="video_url" type="url" placeholder="https://youtube.com/watch?v=…">
        <span class="detect" id="detect"></span>
      </div>
      <div class="field" id="qualityField">
        <label class="f" for="quality">YouTube quality</label>
        <select id="quality">
          <option value="1080" selected>Up to 1080p</option>
          <option value="720">Up to 720p</option>
          <option value="480">Up to 480p</option>
          <option value="best">Best available</option>
        </select>
      </div>
      <label class="toggle">
        <span><b>Loop the video</b><small>Restart when it ends (not for live sources)</small></span>
        <input type="checkbox" id="loop"><span class="sw"></span>
      </label>
      <label class="toggle">
        <span><b>Screen mode</b><small>Shows your Screen-tab text, boxes and images on the live. Re-encodes once for all platforms (uses CPU). Off = direct copy.</small></span>
        <input type="checkbox" id="ovmode"><span class="sw"></span>
      </label>
      <div class="field" id="oqField" hidden>
        <label class="f" for="oq">Screen mode output</label>
        <select id="oq">
          <option value="480" selected>480p, 25fps (lightest)</option>
          <option value="720">720p, 25fps</option>
          <option value="1080">1080p, 25fps (heavy)</option>
        </select>
      </div>
    </section>

    <section class="card">
      <div class="head">
        <div><h2>Stream to</h2><p class="sub" style="margin-bottom:14px" id="pickSub">Tap to choose one or more platforms.</p></div>
        <button type="button" class="mini" id="selAll">Select all</button>
      </div>
      <div class="plist" id="homeList" role="group" aria-label="Platforms"></div>
      <div class="empty" id="homeEmpty" hidden>
        <p>No platforms saved yet.</p>
        <button type="button" class="mini" id="goSettings">Add a platform</button>
      </div>
    </section>

    <div class="actions">
      <button type="button" class="btn" id="run">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4.5v15a1 1 0 0 0 1.5.86l12-7.5a1 1 0 0 0 0-1.72l-12-7.5A1 1 0 0 0 7 4.5z"/></svg>
        <span id="runText">Run</span>
      </button>
      <button type="button" class="btn" id="stop" disabled>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="3"/></svg>
        Stop
      </button>
    </div>

    <details class="card" open>
      <summary>FFmpeg console</summary>
      <pre id="log">Nothing running.</pre>
    </details>

    <p class="note">Once it says ON AIR you can close this tab and go offline. The server keeps streaming. If the source link drops it resumes by itself and your platforms stay connected, so there is no cut on their side.</p>
  </main>

  <!-- ================= SCREEN ================= -->
  <main class="view" id="view-screen">
    <section class="card">
      <div class="head">
        <div><h2>Screen</h2><p class="sub" style="margin-bottom:12px">Drag elements onto the picture. Pinch-free: drag to move, corner dot to resize, top dot to rotate.</p></div>
        <button type="button" class="mini" id="grab">Grab live frame</button>
      </div>
      <div class="badge warn" id="ovBadge"></div>
      <div class="stage"><canvas id="cv"></canvas></div>
      <div class="tools">
        <button type="button" class="chip" data-add="text">+ Text</button>
        <button type="button" class="chip" data-add="rect">+ Rectangle</button>
        <button type="button" class="chip" data-add="round">+ Rounded box</button>
        <button type="button" class="chip" data-add="circle">+ Circle</button>
        <button type="button" class="chip" id="addImg">+ Image</button>
        <button type="button" class="chip" id="clearAll">Clear all</button>
        <input type="file" id="imgFile" accept="image/*" hidden>
      </div>
    </section>

    <section class="card" id="props" hidden>
      <div class="head"><h2 id="propTitle">Selected</h2><button type="button" class="mini danger" id="pDel">Delete</button></div>
      <div class="pr" data-for="text rect round circle">
        <label class="f" for="p_text">Text</label><textarea id="p_text" spellcheck="false"></textarea>
      </div>
      <div class="pgrid">
        <div data-for="text rect round circle"><label class="f" for="p_fs">Text size</label><input type="range" id="p_fs" min="12" max="200"></div>
        <div data-for="text rect round circle"><label class="f" for="p_color">Text color</label><input type="color" id="p_color"></div>
        <div data-for="text rect round circle"><label class="f" for="p_fill">Fill color</label><input type="color" id="p_fill">
          <label class="cb"><input type="checkbox" id="p_fillOn"> Filled</label></div>
        <div><label class="f" for="p_op">Opacity</label><input type="range" id="p_op" min="5" max="100"></div>
        <div><label class="f" for="p_sw">Border width</label><input type="range" id="p_sw" min="0" max="24"></div>
        <div><label class="f" for="p_stroke">Border color</label><input type="color" id="p_stroke"></div>
        <div data-for="rect round"><label class="f" for="p_r">Corner radius</label><input type="range" id="p_r" min="0" max="200"></div>
        <div data-for="text rect round circle"><label class="f" for="p_align">Align</label>
          <select id="p_align"><option value="left">Left</option><option value="center">Center</option><option value="right">Right</option></select>
          <label class="cb"><input type="checkbox" id="p_bold"> Bold</label></div>
      </div>
      <div class="tools" style="margin-top:0">
        <button type="button" class="mini" id="pDup">Duplicate</button>
        <button type="button" class="mini" id="pFront">To front</button>
        <button type="button" class="mini" id="pBack">To back</button>
      </div>
    </section>
  </main>

  <!-- ================= SETTINGS ================= -->
  <main class="view" id="view-settings">
    <section class="card">
      <h2 id="formTitle">Create RTMP</h2>
      <p class="sub">Save a platform once, then pick it on the Home screen.</p>
      <div class="chips" id="chips" role="group" aria-label="Quick fill">
        <button type="button" class="chip" data-name="YouTube" data-url="rtmp://a.rtmp.youtube.com/live2">YouTube</button>
        <button type="button" class="chip" data-name="Facebook" data-url="rtmps://live-api-s.facebook.com:443/rtmp/">Facebook</button>
        <button type="button" class="chip" data-name="Twitch" data-url="rtmp://live.twitch.tv/app">Twitch</button>
        <button type="button" class="chip" data-name="Kick" data-url="rtmps://fa723fc1b171.global-contribute.live-video.net:443/app">Kick</button>
        <button type="button" class="chip" data-name="Rumble" data-url="rtmp://rtmp.rumble.com/live">Rumble</button>
      </div>
      <div class="field">
        <label class="f" for="p_name">Platform name</label>
        <input id="p_name" type="text" placeholder="e.g. Rooter" maxlength="40" autocomplete="off">
      </div>
      <div class="field">
        <label class="f" for="p_url">Stream URL</label>
        <input id="p_url" type="text" placeholder="rtmps://…" autocomplete="off" autocapitalize="off">
      </div>
      <div class="field has-eye">
        <label class="f" for="p_key">Stream key</label>
        <input id="p_key" type="password" placeholder="Paste the key" autocomplete="off">
        <button type="button" class="eye" id="eye" aria-label="Show stream key">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>
        </button>
      </div>
      <div class="actions" style="grid-template-columns: 2fr 1fr">
        <button type="button" class="btn save" id="savePlat">Save platform</button>
        <button type="button" class="btn" id="cancelEdit" style="background:rgba(255,255,255,.08);border:1px solid var(--stroke);color:var(--text)" hidden>Cancel</button>
      </div>
    </section>

    <section class="card">
      <h2>Saved platforms</h2>
      <p class="sub">Keys stay in this browser and go to your server only when you press Run or Test.</p>
      <div class="plist" id="setList"></div>
      <div class="empty" id="setEmpty" hidden><p>Nothing saved yet. Create your first RTMP above.</p></div>
    </section>

    <details class="card" id="cookieBox">
      <summary><span>YouTube access</span> <span id="cookieState">No cookies saved</span></summary>
      <div class="pad">
        <p class="sub" style="margin-bottom:10px">YouTube often blocks cloud servers. Paste your cookies once and this browser remembers them. Use a spare Google account.</p>
        <ol class="steps">
          <li>On a computer (or Kiwi Browser), open a private window and sign in to YouTube.</li>
          <li>Open youtube.com/robots.txt and export the cookies (Cookie-Editor JSON or a cookies.txt file both work).</li>
          <li>Close the private window, then paste the exported text here.</li>
        </ol>
        <textarea id="cookies" placeholder="Paste cookies here" spellcheck="false"></textarea>
        <div class="row"><span class="sub" style="margin:0">Sent only to your server when you press Run.</span>
          <button type="button" class="mini danger" id="clearCookies">Clear cookies</button></div>
      </div>
    </details>
  </main>
</div>

<nav class="nav" aria-label="Main">
  <button type="button" id="nav-home" aria-current="page">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 11l9-8 9 8"/><path d="M5 10v10h5v-6h4v6h5V10"/></svg>
    Home
  </button>
  <button type="button" id="nav-screen">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="12" rx="2.5"/><path d="M8 20h8M12 16v4"/></svg>
    Screen
  </button>
  <button type="button" id="nav-settings">
    <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>
    Settings
  </button>
</nav>
<div id="toast" role="status"></div>

<script>
const $ = id => document.getElementById(id);
const CHECK = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#1b0a12" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
let uptime = 0, fetchedAt = Date.now(), running = false, editingId = null, srvMode = "";
let platforms = [], selected = new Set(), lastDests = [];

const store = {
  get(k) { try { return localStorage.getItem("sd_" + k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem("sd_" + k, v); } catch (e) {} }
};
const pad = n => String(n).padStart(2, "0");
const fmt = s => pad(Math.floor(s / 3600)) + ":" + pad(Math.floor(s % 3600 / 60)) + ":" + pad(Math.floor(s % 60));

function toast(text, kind) {
  const t = $("toast"); t.textContent = text; t.className = "show " + (kind || "");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.className = "", kind === "err" ? 9000 : 4200);
}

// ---------- navigation
const VIEWS = ["home", "screen", "settings"];
const viewFromHash = () => VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "home";
function show(view, push) {
  VIEWS.forEach(v => {
    $("view-" + v).classList.toggle("active", v === view);
    const b = $("nav-" + v); if (v === view) b.setAttribute("aria-current", "page"); else b.removeAttribute("aria-current");
  });
  if (push !== false) history.replaceState(null, "", "#" + view);
  window.scrollTo({ top: 0 });
  if (view === "screen" && window.onScreen) window.onScreen();
}
$("nav-home").addEventListener("click", () => show("home"));
$("nav-screen").addEventListener("click", () => show("screen"));
$("nav-settings").addEventListener("click", () => show("settings"));
$("goSettings").addEventListener("click", () => show("settings"));
window.addEventListener("hashchange", () => show(viewFromHash(), false));

// ---------- platforms
function loadPlatforms() {
  try { platforms = JSON.parse(store.get("platforms") || "null"); } catch (e) { platforms = null; }
  if (!Array.isArray(platforms)) {
    platforms = [];
    const u = store.get("stream_url"), k = store.get("stream_key");   // migrate the old single destination
    if (u && k) platforms.push({ id: newId(), name: "My platform", url: u, key: k });
    savePlatforms();
  }
  let sel = []; try { sel = JSON.parse(store.get("selected") || "[]"); } catch (e) {}
  selected = new Set(sel.filter(id => platforms.some(p => p.id === id)));
}
const newId = () => Math.random().toString(36).slice(2, 10);
function savePlatforms() { store.set("platforms", JSON.stringify(platforms)); store.set("selected", JSON.stringify([...selected])); }
function hostOf(u) { try { return new URL(u.replace(/^rtmps?:/, "https:")).host; } catch (e) { return u; } }
function avatar(name) {
  let h = 0; for (const c of name) h = (h * 31 + c.charCodeAt(0)) % 360;
  const el = document.createElement("span"); el.className = "av"; el.setAttribute("aria-hidden", "true");
  el.style.background = "linear-gradient(135deg, hsl(" + h + " 85% 64%), hsl(" + ((h + 45) % 360) + " 85% 56%))";
  el.textContent = (name.trim()[0] || "?").toUpperCase(); return el;
}

function renderHome() {
  const list = $("homeList"); list.textContent = "";
  $("homeEmpty").hidden = platforms.length > 0; $("selAll").hidden = platforms.length < 2;
  const st = Object.fromEntries(lastDests.map(d => [d.id, d]));
  platforms.forEach(p => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "plat"; b.dataset.id = p.id;
    b.setAttribute("role", "checkbox"); b.setAttribute("aria-checked", selected.has(p.id));
    b.setAttribute("aria-disabled", running);
    const pt = document.createElement("span"); pt.className = "pt";
    const nm = document.createElement("b"); nm.textContent = p.name;
    const sm = document.createElement("small"); sm.textContent = hostOf(p.url); sm.dataset.id = p.id;
    const d = running && st[p.id];
    if (d) {
      sm.dataset.st = !d.active ? "off" : d.phase === "reconnecting" ? "retry" : "live";
      sm.textContent = !d.active ? "Stopped" : (d.phase === "reconnecting" ? "Reconnecting…" : "Live")
        + (d.restarts ? " (reconnected " + d.restarts + "x)" : "");
    }
    pt.append(nm, sm);
    const ck = document.createElement("span"); ck.className = "ck"; ck.innerHTML = CHECK;
    b.append(avatar(p.name), pt, ck);
    b.addEventListener("click", () => {
      if (running) { toast("Stop the stream before changing platforms."); return; }
      selected.has(p.id) ? selected.delete(p.id) : selected.add(p.id);
      savePlatforms(); renderHome();
    });
    list.append(b);
  });
  const n = selected.size;
  $("runText").textContent = n ? "Run on " + n + (n === 1 ? " platform" : " platforms") : "Run";
  $("selAll").textContent = n === platforms.length ? "Clear all" : "Select all";
}

function renderSettings() {
  const list = $("setList"); list.textContent = "";
  $("setEmpty").hidden = platforms.length > 0;
  platforms.forEach(p => {
    const row = document.createElement("div"); row.className = "srow";
    const top = document.createElement("div"); top.className = "top2";
    const pt = document.createElement("span"); pt.className = "pt";
    const nm = document.createElement("b"); nm.textContent = p.name;
    const sm = document.createElement("small"); sm.textContent = hostOf(p.url);
    pt.append(nm, sm); top.append(avatar(p.name), pt);
    const btns = document.createElement("div"); btns.className = "btns";
    const mk = (label, cls, fn) => { const x = document.createElement("button"); x.type = "button"; x.className = "mini " + cls; x.textContent = label; x.addEventListener("click", fn); btns.append(x); return x; };
    const t = mk("Send test", "test", () => sendTest(p)); t.disabled = running;
    mk("Edit", "", () => editPlatform(p));
    mk("Delete", "danger", () => {
      if (!confirm("Delete " + p.name + "?")) return;
      platforms = platforms.filter(x => x.id !== p.id); selected.delete(p.id);
      savePlatforms(); renderAll(); toast("Deleted.", "ok");
    });
    row.append(top, btns); list.append(row);
  });
}
function renderAll() { renderHome(); renderSettings(); }

function resetForm() {
  editingId = null; ["p_name", "p_url", "p_key"].forEach(i => $(i).value = "");
  $("formTitle").textContent = "Create RTMP"; $("savePlat").textContent = "Save platform"; $("cancelEdit").hidden = true;
}
function editPlatform(p) {
  editingId = p.id; $("p_name").value = p.name; $("p_url").value = p.url; $("p_key").value = p.key;
  $("formTitle").textContent = "Edit " + p.name; $("savePlat").textContent = "Update platform"; $("cancelEdit").hidden = false;
  $("formTitle").scrollIntoView({ behavior: "smooth", block: "start" });
}
$("cancelEdit").addEventListener("click", resetForm);

$("savePlat").addEventListener("click", () => {
  const name = $("p_name").value.trim(), url = $("p_url").value.trim(), key = $("p_key").value.trim();
  if (!name) return toast("Give the platform a name.", "err");
  if (!/^rtmps?:\/\/.+/i.test(url)) return toast("Stream URL must start with rtmp:// or rtmps://", "err");
  if (!key) return toast("Paste the stream key.", "err");
  if (editingId) {
    platforms = platforms.map(p => p.id === editingId ? { ...p, name, url, key } : p); toast("Updated.", "ok");
  } else {
    const p = { id: newId(), name, url, key }; platforms.push(p); selected.add(p.id); toast(name + " saved and selected.", "ok");
  }
  savePlatforms(); resetForm(); renderAll();
});

$("chips").addEventListener("click", e => {
  const c = e.target.closest(".chip"); if (!c) return;
  const names = [...document.querySelectorAll("#chips .chip")].map(x => x.dataset.name);
  $("p_url").value = c.dataset.url;
  if (!$("p_name").value.trim() || names.includes($("p_name").value.trim())) $("p_name").value = c.dataset.name;
  $("p_key").focus();
});
$("eye").addEventListener("click", () => {
  const k = $("p_key"), shown = k.type === "password"; k.type = shown ? "text" : "password";
  $("eye").setAttribute("aria-label", shown ? "Hide stream key" : "Show stream key");
});
$("selAll").addEventListener("click", () => {
  if (running) return;
  selected = selected.size === platforms.length ? new Set() : new Set(platforms.map(p => p.id));
  savePlatforms(); renderHome();
});

// ---------- source + cookies (remembered across refreshes)
["video_url", "quality", "cookies", "oq"].forEach(k => ["input", "change"].forEach(ev => $(k).addEventListener(ev, () => {
  store.set(k, $(k).value); if (k === "cookies") cookieStatus();
})));
$("loop").addEventListener("change", () => store.set("loop", $("loop").checked ? "1" : "0"));
$("ovmode").addEventListener("change", () => {
  store.set("ovmode", $("ovmode").checked ? "1" : "0"); $("oqField").hidden = !$("ovmode").checked; updateBadge();
});
$("video_url").addEventListener("input", e => {
  const v = e.target.value, d = $("detect");
  const yt = /(youtube\.com|youtu\.be)/i.test(v), dr = /(drive|docs)\.google\.com/i.test(v);
  d.className = "detect " + (yt ? "yt" : dr ? "dr" : ""); d.textContent = yt ? "YouTube" : dr ? "Drive" : "";
  $("qualityField").style.display = dr ? "none" : "";
});
function cookieStatus() {
  const on = $("cookies").value.trim().length > 20;
  $("cookieState").textContent = on ? "Cookies saved in this browser" : "No cookies saved"; $("cookieState").className = on ? "on" : "";
}
$("clearCookies").addEventListener("click", () => { $("cookies").value = ""; store.set("cookies", ""); cookieStatus(); toast("Cookies cleared.", "ok"); });

// ---------- talking to the server
const pickDests = list => list.map(({ id, name, url, key }) => ({ id, name, url, key }));

async function post(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  return { ok: res.ok, data: await res.json().catch(() => ({})) };
}
async function sendTest(p) {
  toast("Sending a 30-second test pattern to " + p.name + "…");
  try {
    const r = await post("/start", { test: true, destinations: pickDests([p]) });
    if (r.ok) { toast("Test started. Check " + p.name + "’s preview now.", "ok"); show("home"); }
    else toast(r.data.error || "Couldn’t start the test.", "err");
  } catch (e) { toast("Couldn’t reach the server.", "err"); }
  refresh();
}
$("run").addEventListener("click", async () => {
  if (!$("video_url").value.trim()) { toast("Paste a YouTube or Drive link first.", "err"); $("video_url").focus(); return; }
  if (!selected.size) { toast(platforms.length ? "Tap at least one platform to stream to." : "Add a platform in Settings first.", "err"); return; }
  $("run").disabled = true; toast("Fetching the video and starting FFmpeg…");
  if ($("ovmode").checked) await pushOverlay();
  try {
    const r = await post("/start", { video_url: $("video_url").value, quality: $("quality").value, loop: $("loop").checked,
      cookies: $("cookies").value, overlay: $("ovmode").checked, oq: $("oq").value,
      destinations: pickDests(platforms.filter(p => selected.has(p.id))) });
    if (r.ok) toast("You’re live on " + r.data.count + (r.data.count === 1 ? " platform" : " platforms") + ". You can close this tab.", "ok");
    else {
      toast(r.data.error || "Couldn’t start the stream.", "err");
      if (r.data.need_cookies) { show("settings"); $("cookieBox").open = true; $("cookieBox").scrollIntoView({ behavior: "smooth", block: "center" }); }
    }
  } catch (err) { toast("Couldn’t reach the server.", "err"); }
  refresh();
});
$("stop").addEventListener("click", async () => { await post("/stop"); toast("Stopped.", "ok"); refresh(); });

function tick() { $("timer").textContent = fmt(running ? uptime + Math.floor((Date.now() - fetchedAt) / 1000) : 0); }
async function refresh() {
  try {
    const r = await (await fetch("/status")).json();
    const was = running; running = r.running; uptime = r.uptime; fetchedAt = Date.now(); lastDests = r.dests || [];
    const live = lastDests.filter(d => d.active && d.phase === "streaming").length, total = lastDests.filter(d => d.active).length;
    $("root").classList.toggle("live", r.running);
    $("state").textContent = !r.running ? "OFFLINE" : live ? "ON AIR" : "RETRYING";
    $("run").disabled = r.running; $("stop").disabled = !r.running;
    $("nowLabel").textContent = r.running ? "Now streaming" : (lastDests.length ? "Last stream ended" : "Nothing is streaming");
    $("nowTitle").textContent = r.title || "Ready when you are";
    $("pillText").textContent = !r.running ? "Server idle" : (live ? "Live on " : "Reconnecting ") + (live || total) + " of " + total + (total === 1 ? " platform" : " platforms");
    srvMode = r.running ? r.mode : ""; updateBadge();
    const bits = [];
    if (r.running) {
      bits.push(r.mode === "encode" ? "Screen mode on" : r.mode === "test" ? "Test pattern" : "Direct copy");
      if (r.duration) bits.push("Played " + fmt(r.pos) + " of " + fmt(r.duration));
      if (r.src_phase === "reconnecting") bits.push("Source reconnecting, platforms held");
      else if (r.restarts) bits.push("Source resumed " + r.restarts + "×, platforms stayed connected");
    }
    $("prog").textContent = bits.join(" · ");
    if (r.logs.length) {
      const el = $("log"), atEnd = el.scrollTop + el.clientHeight >= el.scrollHeight - 24;
      el.textContent = r.logs.join("\n"); if (atEnd) el.scrollTop = el.scrollHeight;
    }
    const sig = running ? JSON.stringify(lastDests) : "idle";   // re-draw only when something changed,
    if (sig !== refresh.sig) { refresh.sig = sig; renderHome(); }   // so taps are never lost to a refresh
    if (was !== running) renderSettings();
    tick();
  } catch (e) { /* offline is fine: the server keeps streaming */ }
}

// ================= screen editor =================
const W = 1280, H = 720;
const cv = $("cv"), cx = cv.getContext("2d");
cv.width = W; cv.height = H;
let scene = [], selId = null, bgImg = null, drag = null, lastGrab = "", pushT = null;
const imgs = {};
const cur = () => scene.find(e => e.id === selId) || null;
const pxs = () => (cv.getBoundingClientRect().width / W) || 1;
const handleR = () => Math.max(14, 15 / pxs());

function imgFor(e) {
  let im = imgs[e.id];
  if (!im || im._src !== e.src) { im = new Image(); im._src = e.src; im.onload = () => { render(); schedulePush(); }; im.src = e.src; imgs[e.id] = im; }
  return im;
}
function rr(c, x, y, w, h, r) {
  c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r); c.arcTo(x + w, y + h, x, y + h, r);
  c.arcTo(x, y + h, x, y, r); c.arcTo(x, y, x + w, y, r); c.closePath();
}
function wrapText(c, text, maxW) {
  const out = [];
  String(text).split("\n").forEach(par => {
    let line = "";
    par.split(" ").forEach(word => {
      const t = line ? line + " " + word : word;
      if (c.measureText(t).width > maxW && line) { out.push(line); line = word; } else line = t;
    });
    out.push(line);
  });
  return out;
}
function drawText(c, e) {
  c.font = (e.bold ? "700 " : "400 ") + e.fs + 'px "Space Grotesk", system-ui, sans-serif';
  c.fillStyle = e.color; c.textBaseline = "middle"; c.textAlign = e.align;
  const lines = wrapText(c, e.text, Math.max(10, e.w - 24)), lh = e.fs * 1.2;
  let y = -(lines.length * lh) / 2 + lh / 2;
  const x = e.align === "left" ? -e.w / 2 + 12 : e.align === "right" ? e.w / 2 - 12 : 0;
  lines.forEach(l => { c.fillText(l, x, y); y += lh; });
}
function drawEl(c, e) {
  c.save(); c.translate(e.x, e.y); c.rotate(e.rot * Math.PI / 180); c.globalAlpha = e.op;
  const w = e.w, h = e.h;
  if (e.type === "image") {
    const im = imgFor(e);
    if (im.complete && im.naturalWidth) c.drawImage(im, -w / 2, -h / 2, w, h);
    if (e.sw > 0) { c.lineWidth = e.sw; c.strokeStyle = e.stroke; c.strokeRect(-w / 2, -h / 2, w, h); }
  } else {
    c.beginPath();
    if (e.type === "circle") c.ellipse(0, 0, w / 2, h / 2, 0, 0, Math.PI * 2);
    else rr(c, -w / 2, -h / 2, w, h, e.type === "round" || e.type === "rect" ? Math.min(e.r, w / 2, h / 2) : 0);
    if (e.fill) { c.fillStyle = e.fill; c.fill(); }
    if (e.sw > 0) { c.lineWidth = e.sw; c.strokeStyle = e.stroke; c.stroke(); }
    if (e.text) drawText(c, e);
  }
  c.restore();
}
function drawSel(e) {
  const hr = handleR(), s = pxs(), ry = -e.h / 2 - hr * 2.6;
  cx.save(); cx.translate(e.x, e.y); cx.rotate(e.rot * Math.PI / 180);
  cx.strokeStyle = "#36e2ff"; cx.lineWidth = 2 / s; cx.setLineDash([8 / s, 6 / s]);
  cx.strokeRect(-e.w / 2, -e.h / 2, e.w, e.h); cx.setLineDash([]);
  cx.beginPath(); cx.moveTo(0, -e.h / 2); cx.lineTo(0, ry); cx.stroke();
  cx.fillStyle = "#fff";
  [[e.w / 2, e.h / 2], [0, ry]].forEach(([x, y]) => { cx.beginPath(); cx.arc(x, y, hr * .8, 0, 7); cx.fill(); cx.stroke(); });
  cx.restore();
}
function render() {
  cx.globalAlpha = 1; cx.fillStyle = "#0b0720"; cx.fillRect(0, 0, W, H);
  if (bgImg) { const k = Math.min(W / bgImg.width, H / bgImg.height), w = bgImg.width * k, h = bgImg.height * k; cx.drawImage(bgImg, (W - w) / 2, (H - h) / 2, w, h); }
  else { cx.fillStyle = "rgba(255,255,255,.35)"; cx.font = '28px "Space Grotesk", sans-serif'; cx.textAlign = "center"; cx.textBaseline = "middle"; cx.fillText("Tap “Grab live frame” to see your video here", W / 2, H / 2); }
  scene.forEach(e => drawEl(cx, e));
  const s = cur(); if (s) drawSel(s);
}

function updateBadge() {
  const b = $("ovBadge"); let t, c;
  if (running && srvMode === "encode") { t = "LIVE: your changes appear on the stream within about a second."; c = "on"; }
  else if (running) { t = "This stream is in direct-copy mode, so elements won’t show. Stop, turn on Screen mode on Home, and run again."; c = "warn"; }
  else if ($("ovmode").checked) { t = "Screen mode is on: these elements will show on the live when you press Run."; c = "on"; }
  else { t = "Screen mode is off. Turn it on at Home to put these elements on the live."; c = "warn"; }
  b.textContent = t; b.className = "badge " + c;
}

function persist() { store.set("scene", JSON.stringify(scene)); }
function changed() { persist(); render(); schedulePush(); }
function overlayBlob() {
  const oc = document.createElement("canvas"); oc.width = W; oc.height = H;
  const c = oc.getContext("2d"); scene.forEach(e => drawEl(c, e));
  return new Promise(res => oc.toBlob(res, "image/png"));
}
async function pushOverlay() {
  try { const b = await overlayBlob(); await fetch("/overlay", { method: "POST", headers: { "Content-Type": "image/png" }, body: b }); } catch (e) {}
}
function schedulePush() {
  if (!(running && srvMode === "encode") || pushT) return;
  pushT = setTimeout(() => { pushT = null; pushOverlay(); }, 400);
}

function addEl(type, extra) {
  const e = Object.assign({ id: newId(), type, x: W / 2, y: H / 2, w: 360, h: 120, rot: 0, op: 1,
    fill: "#ff3366", stroke: "#ffffff", sw: 0, text: "", color: "#ffffff", fs: 48, bold: true, r: 0, align: "center" }, extra || {});
  if (type === "text") { e.text = "Your text"; e.fill = ""; e.w = 480; e.h = 90; }
  if (type === "round") { e.r = 40; e.text = "Text"; }
  if (type === "circle") { e.w = e.h = 200; }
  scene.push(e); selId = e.id; changed(); renderProps();
}
document.querySelectorAll("[data-add]").forEach(b => b.addEventListener("click", () => addEl(b.dataset.add)));
$("clearAll").addEventListener("click", () => { if (!scene.length || !confirm("Remove every element?")) return; scene = []; selId = null; changed(); renderProps(); });

$("addImg").addEventListener("click", () => $("imgFile").click());
$("imgFile").addEventListener("change", ev => {
  const f = ev.target.files[0]; ev.target.value = ""; if (!f) return;
  const rd = new FileReader();
  rd.onload = () => {
    const im = new Image();
    im.onload = () => {
      const k = Math.min(1, 1280 / Math.max(im.width, im.height)), c = document.createElement("canvas");
      c.width = Math.round(im.width * k); c.height = Math.round(im.height * k);
      c.getContext("2d").drawImage(im, 0, 0, c.width, c.height);
      const keepAlpha = /png|gif|webp|svg/.test(f.type);
      const url = c.toDataURL(keepAlpha ? "image/png" : "image/jpeg", .9);
      const fit = Math.min(400 / c.width, 300 / c.height, 1.5);
      addEl("image", { src: url, w: Math.round(c.width * fit), h: Math.round(c.height * fit) });
    };
    im.src = rd.result;
  };
  rd.readAsDataURL(f);
});

// pointer: move, resize (corner dot), rotate (top dot)
const pt = ev => { const r = cv.getBoundingClientRect(); return { x: (ev.clientX - r.left) * W / r.width, y: (ev.clientY - r.top) * H / r.height }; };
function toLocal(e, p) {
  const a = -e.rot * Math.PI / 180, dx = p.x - e.x, dy = p.y - e.y;
  return { x: dx * Math.cos(a) - dy * Math.sin(a), y: dx * Math.sin(a) + dy * Math.cos(a) };
}
cv.addEventListener("pointerdown", ev => {
  const p = pt(ev), hr = handleR(), s = cur();
  drag = null;
  if (s) {
    const l = toLocal(s, p);
    if (Math.hypot(l.x - s.w / 2, l.y - s.h / 2) < hr * 1.6) drag = { mode: "scale", id: s.id, ratio: s.w / s.h };
    else if (Math.hypot(l.x, l.y + s.h / 2 + hr * 2.6) < hr * 1.6) drag = { mode: "rot", id: s.id };
  }
  if (!drag) {
    let hit = null;
    for (let i = scene.length - 1; i >= 0; i--) { const e = scene[i], l = toLocal(e, p); if (Math.abs(l.x) <= e.w / 2 && Math.abs(l.y) <= e.h / 2) { hit = e; break; } }
    selId = hit ? hit.id : null;
    if (hit) drag = { mode: "move", id: hit.id, dx: p.x - hit.x, dy: p.y - hit.y };
    renderProps();
  }
  if (drag) { try { cv.setPointerCapture(ev.pointerId); } catch (e) {} }
  render(); ev.preventDefault();
});
cv.addEventListener("pointermove", ev => {
  if (!drag) return;
  const e = scene.find(x => x.id === drag.id); if (!e) return;
  const p = pt(ev); drag.moved = true;
  if (drag.mode === "move") { e.x = p.x - drag.dx; e.y = p.y - drag.dy; }
  else if (drag.mode === "scale") {
    const l = toLocal(e, p); e.w = Math.max(24, Math.abs(l.x) * 2);
    e.h = e.type === "image" ? e.w / drag.ratio : Math.max(24, Math.abs(l.y) * 2);
  } else {
    let r = Math.atan2(p.y - e.y, p.x - e.x) * 180 / Math.PI + 90; r = ((r % 360) + 360) % 360;
    const sn = Math.round(r / 90) * 90; if (Math.abs(r - sn) < 4) r = sn % 360;
    e.rot = r;
  }
  render(); schedulePush();
});
const endDrag = () => { if (drag && drag.moved) { persist(); schedulePush(); } drag = null; };
cv.addEventListener("pointerup", endDrag); cv.addEventListener("pointercancel", endDrag);
window.addEventListener("keydown", ev => {
  if ((ev.key === "Delete" || ev.key === "Backspace") && cur() && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && $("view-screen").classList.contains("active")) {
    scene = scene.filter(e => e.id !== selId); selId = null; changed(); renderProps(); ev.preventDefault();
  }
});

// properties panel
const PM = { p_text: ["text", "s"], p_fs: ["fs", "n"], p_color: ["color", "s"], p_sw: ["sw", "n"], p_stroke: ["stroke", "s"], p_r: ["r", "n"], p_op: ["op", "o"], p_align: ["align", "s"] };
Object.entries(PM).forEach(([id, [k, t]]) => $(id).addEventListener("input", () => {
  const e = cur(); if (!e) return; const v = $(id).value;
  e[k] = t === "n" ? +v : t === "o" ? v / 100 : v; changed();
}));
$("p_fill").addEventListener("input", () => { const e = cur(); if (!e) return; e.fill = $("p_fill").value; $("p_fillOn").checked = true; changed(); });
$("p_fillOn").addEventListener("change", () => { const e = cur(); if (!e) return; e.fill = $("p_fillOn").checked ? $("p_fill").value : ""; changed(); });
$("p_bold").addEventListener("change", () => { const e = cur(); if (!e) return; e.bold = $("p_bold").checked; changed(); });
$("pDel").addEventListener("click", () => { scene = scene.filter(e => e.id !== selId); selId = null; changed(); renderProps(); });
$("pDup").addEventListener("click", () => { const e = cur(); if (!e) return; const c = { ...e, id: newId(), x: e.x + 30, y: e.y + 30 }; scene.push(c); selId = c.id; changed(); renderProps(); });
$("pFront").addEventListener("click", () => { const e = cur(); if (!e) return; scene = scene.filter(x => x !== e).concat(e); changed(); });
$("pBack").addEventListener("click", () => { const e = cur(); if (!e) return; scene = [e].concat(scene.filter(x => x !== e)); changed(); });

function renderProps() {
  const e = cur(); $("props").hidden = !e; if (!e) return;
  const names = { text: "Text", rect: "Rectangle", round: "Rounded box", circle: "Circle", image: "Image" };
  $("propTitle").textContent = names[e.type] || "Selected";
  document.querySelectorAll("#props [data-for]").forEach(el => el.hidden = !el.dataset.for.split(" ").includes(e.type));
  $("p_text").value = e.text || ""; $("p_fs").value = e.fs; $("p_color").value = e.color;
  $("p_fill").value = e.fill || "#ff3366"; $("p_fillOn").checked = !!e.fill;
  $("p_op").value = Math.round(e.op * 100); $("p_sw").value = e.sw; $("p_stroke").value = e.stroke;
  $("p_r").value = e.r; $("p_align").value = e.align; $("p_bold").checked = !!e.bold;
}

// live frame
async function grab() {
  const u = $("video_url").value.trim();
  if (!u && !running) { toast("Paste the video link on Home first.", "err"); return; }
  lastGrab = u; $("grab").disabled = true; toast("Grabbing a frame from the video…");
  try {
    const res = await fetch("/snapshot", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ video_url: u, quality: $("quality").value, cookies: $("cookies").value }) });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      toast(j.error || ("Server didn’t answer in time (code " + res.status + "). It may be busy encoding."), "err");
      if (j.need_cookies) { show("settings"); $("cookieBox").open = true; }
    } else {
      const im = new Image(); im.onload = () => { bgImg = im; render(); toast("Screen updated.", "ok"); };
      im.src = URL.createObjectURL(await res.blob());
    }
  } catch (e) { toast("Couldn’t reach the server.", "err"); }
  $("grab").disabled = false;
}
$("grab").addEventListener("click", grab);
window.onScreen = () => {
  render(); updateBadge();
  const u = $("video_url").value.trim();
  if (u && u !== lastGrab && !bgImg) grab();
};

// ---------- boot
["video_url", "quality", "cookies", "oq"].forEach(k => { const v = store.get(k); if (v !== null) $(k).value = v; });
$("loop").checked = store.get("loop") === "1";
$("ovmode").checked = store.get("ovmode") === "1"; $("oqField").hidden = !$("ovmode").checked;
try { const s = JSON.parse(store.get("scene") || "[]"); scene = Array.isArray(s) ? s : []; } catch (e) { scene = []; }
$("video_url").dispatchEvent(new Event("input")); cookieStatus();
loadPlatforms(); renderAll(); render(); updateBadge(); renderProps();
if (document.fonts && document.fonts.ready) document.fonts.ready.then(render);
window.addEventListener("resize", render);
show(viewFromHash(), false);
refresh(); setInterval(refresh, 3000); setInterval(tick, 1000);
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
