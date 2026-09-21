#!/usr/bin/env python3
"""discord-rpc — show the `$>` typing-dots GIF as Chun's Discord Rich Presence.

Standard library only. Talks to the Discord desktop app over its local IPC
socket (the official Rich Presence route; no account token is involved).

    presence.py            run forever (what launchd starts)
    presence.py --check    connect, set the presence, hold it 20 s, exit 0/1
    presence.py --reset-timer   start the elapsed timer again from now

The presence is set ONCE per connection and simply held, so Discord's
rate limit (5 updates / 20 s) is never approached. The GIF does the animating.
"""
import argparse
import json
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import time
import uuid

CLIENT_ID = "1551699389263904769"
GIF_URL = ("https://raw.githubusercontent.com/gthmhttn/discord-rpc/"
           "8f45e02d4a4dcf80e3b61f289bfbabd72bc4fb1e/typing.gif")
HOVER_TEXT = "$>"

# "persist" = the timer keeps counting across restarts and reboots.
# "login"   = the timer starts again each time this script starts.
TIMER_MODE = os.environ.get("DISCORD_RPC_TIMER", "persist")

STATE_DIR = os.path.expanduser(
    os.environ.get("DISCORD_RPC_STATE_DIR",
                   "~/Library/Application Support/discord-rpc"))
RETRY_SECONDS = float(os.environ.get("DISCORD_RPC_RETRY", "15"))
READ_TIMEOUT = 5.0    # short, so a stop signal is noticed within seconds

OP_HANDSHAKE, OP_FRAME, OP_CLOSE, OP_PING, OP_PONG = 0, 1, 2, 3, 4

log = logging.getLogger("discord-rpc")


class ProtocolError(Exception):
    """Discord answered, but not the way the protocol says it should."""


# ---------------------------------------------------------------- framing
def encode(op, payload):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return struct.pack("<II", op, len(body)) + body


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            if buf:          # mid-frame: keep waiting for the rest
                continue
            raise
        if not chunk:
            raise ConnectionError("Discord closed the connection")
        buf += chunk
    return buf


def read_frame(sock):
    op, length = struct.unpack("<II", _recv_exact(sock, 8))
    if length > 1 << 20:
        raise ProtocolError("frame of %d bytes is not plausible" % length)
    body = _recv_exact(sock, length) if length else b"{}"
    try:
        return op, json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError("unreadable frame: %s" % exc)


# ---------------------------------------------------------------- socket
def ipc_dirs():
    """Folders Discord may put discord-ipc-N in, most likely first."""
    dirs = []
    override = os.environ.get("DISCORD_IPC_DIR")
    if override:
        return [override]
    # macOS: the per-user temp dir. launchd agents do not always get TMPDIR,
    # so ask the OS for it directly.
    try:
        dirs.append(os.confstr("CS_DARWIN_USER_TEMP_DIR"))
    except (ValueError, OSError):
        pass
    try:
        out = subprocess.run(["getconf", "DARWIN_USER_TEMP_DIR"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            dirs.append(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    for var in ("TMPDIR", "XDG_RUNTIME_DIR"):
        dirs.append(os.environ.get(var))
    dirs.append("/tmp")
    seen, result = set(), []
    for d in dirs:
        if not d:
            continue
        d = os.path.normpath(d)
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            result.append(d)
    return result


def connect():
    last = None
    for d in ipc_dirs():
        for i in range(10):
            path = os.path.join(d, "discord-ipc-%d" % i)
            if not os.path.exists(path):
                continue
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(10)
            try:
                sock.connect(path)
                return sock, path
            except OSError as exc:
                last = exc
                sock.close()
    raise ConnectionError("Discord is not running (no IPC socket found)"
                          + ("; last error: %s" % last if last else ""))


# ---------------------------------------------------------------- presence
def start_time_ms(now=None):
    now = time.time() if now is None else now
    if TIMER_MODE != "persist":
        return int(now * 1000)
    path = os.path.join(STATE_DIR, "state.json")
    try:
        with open(path) as fh:
            start = int(json.load(fh)["start_ms"])
        if 0 < start <= now * 1000:
            return start
    except (OSError, ValueError, KeyError, TypeError):
        pass
    start = int(now * 1000)
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({"start_ms": start}, fh)
    os.replace(tmp, path)
    return start


def build_activity(start_ms):
    return {
        "assets": {"large_image": GIF_URL, "large_text": HOVER_TEXT},
        "timestamps": {"start": start_ms},
        "instance": False,
    }


def handshake(sock, client_id=CLIENT_ID):
    sock.sendall(encode(OP_HANDSHAKE, {"v": 1, "client_id": client_id}))
    op, msg = read_frame(sock)
    if op == OP_CLOSE:
        raise ProtocolError("Discord refused the handshake: %s"
                            % msg.get("message", msg))
    if msg.get("evt") != "READY":
        raise ProtocolError("expected READY, got %r" % (msg,))
    return msg


def set_activity(sock, activity):
    nonce = str(uuid.uuid4())
    sock.sendall(encode(OP_FRAME, {
        "cmd": "SET_ACTIVITY",
        "args": {"pid": os.getpid(), "activity": activity},
        "nonce": nonce,
    }))
    while True:
        op, msg = read_frame(sock)
        if op == OP_PING:
            sock.sendall(encode(OP_PONG, msg))
            continue
        if op == OP_CLOSE:
            raise ProtocolError("Discord closed: %s" % msg.get("message", msg))
        if msg.get("nonce") != nonce:
            continue                       # unrelated event
        if msg.get("evt") == "ERROR":
            data = msg.get("data") or {}
            raise ProtocolError("SET_ACTIVITY rejected: %s"
                                % data.get("message", data))
        return msg


def hold(sock, stop, deadline=None):
    """Keep the connection (and so the presence) alive until it drops."""
    sock.settimeout(1.0 if deadline else READ_TIMEOUT)
    while not stop["flag"]:
        if deadline and time.time() >= deadline:
            return "deadline"
        try:
            op, msg = read_frame(sock)
        except socket.timeout:
            continue
        if op == OP_PING:
            sock.sendall(encode(OP_PONG, msg))
        elif op == OP_CLOSE:
            return "closed by Discord"
    return "stopped"


def session(stop, deadline=None):
    sock, path = connect()
    try:
        ready = handshake(sock)
        user = (ready.get("data") or {}).get("user") or {}
        set_activity(sock, build_activity(start_time_ms()))
        log.info("presence set via %s (user %s)", path,
                 user.get("username", "?"))
        return hold(sock, stop, deadline)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def run_forever(stop, sleep=time.sleep):
    last_problem = None
    while not stop["flag"]:
        try:
            why = session(stop)
            last_problem = None
            if not stop["flag"]:
                log.info("connection ended (%s); reconnecting", why)
        except (OSError, ConnectionError, ProtocolError) as exc:
            msg = str(exc)
            if msg != last_problem:        # log a problem once, not every retry
                log.warning("%s — retrying every %ds", msg, RETRY_SECONDS)
                last_problem = msg
        waited = 0.0
        while waited < RETRY_SECONDS and not stop["flag"]:
            sleep(0.5)
            waited += 0.5


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true",
                   help="set the presence, hold it 20 s, exit 0 if it worked")
    p.add_argument("--reset-timer", action="store_true",
                   help="start the elapsed timer again from now")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.reset_timer:
        try:
            os.remove(os.path.join(STATE_DIR, "state.json"))
        except FileNotFoundError:
            pass
        start_time_ms()
        print("timer reset; restart the agent to show it "
              "(launchctl kickstart -k gui/$(id -u)/com.chun.discord-rpc)")
        return 0

    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.check:
        try:
            session(stop, deadline=time.time() + 20)
        except (OSError, ConnectionError, ProtocolError) as exc:
            print("check FAILED: %s" % exc)
            return 1
        print("check OK: presence was set and held for 20 s")
        return 0

    log.info("discord-rpc starting (app %s, timer %s)", CLIENT_ID, TIMER_MODE)
    run_forever(stop)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
