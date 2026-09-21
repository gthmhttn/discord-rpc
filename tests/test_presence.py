"""Tests for presence.py against a fake Discord IPC server (unix socket)."""
import importlib
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def load(tmp, **env):
    os.environ["DISCORD_IPC_DIR"] = tmp
    os.environ["DISCORD_RPC_STATE_DIR"] = os.path.join(tmp, "state")
    os.environ["DISCORD_RPC_RETRY"] = "0.5"
    os.environ.pop("DISCORD_RPC_TIMER", None)
    os.environ.update(env)
    import presence
    return importlib.reload(presence)


def frame(op, obj):
    b = json.dumps(obj).encode()
    return struct.pack("<II", op, len(b)) + b


def read(conn):
    hdr = b""
    while len(hdr) < 8:
        c = conn.recv(8 - len(hdr))
        if not c:
            return None, None
        hdr += c
    op, n = struct.unpack("<II", hdr)
    body = b""
    while len(body) < n:
        body += conn.recv(n - len(body))
    return op, json.loads(body)


class FakeDiscord:
    """Accepts connections on discord-ipc-0 and records what clients send.

    behaviour: "ok" | "reject_handshake" | "error_activity" | "ping_then_close"
    """

    def __init__(self, folder, behaviour="ok", name="discord-ipc-0"):
        self.path = os.path.join(folder, name)
        self.behaviour = behaviour
        self.received = []
        self.connections = 0
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        self.srv.listen(5)
        self.srv.settimeout(0.2)
        self.live = []
        self.running = True
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def _loop(self):
        while self.running:
            try:
                conn, _ = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        self.live.append(conn)
        try:
            op, hs = read(conn)
            self.received.append((op, hs))
            if self.behaviour == "reject_handshake":
                conn.sendall(frame(2, {"code": 4000, "message": "Invalid Client ID"}))
                return
            conn.sendall(frame(1, {"cmd": "DISPATCH", "evt": "READY",
                                   "data": {"v": 1, "user": {"username": "chun"}}}))
            op, cmd = read(conn)
            self.received.append((op, cmd))
            # an unrelated event first: the client must skip it
            conn.sendall(frame(1, {"cmd": "DISPATCH", "evt": "OTHER", "nonce": None}))
            if self.behaviour == "error_activity":
                conn.sendall(frame(1, {"cmd": "SET_ACTIVITY", "evt": "ERROR",
                                       "nonce": cmd["nonce"],
                                       "data": {"code": 4000, "message": "bad asset"}}))
                return
            conn.sendall(frame(1, {"cmd": "SET_ACTIVITY", "evt": None,
                                   "nonce": cmd["nonce"], "data": cmd["args"]["activity"]}))
            if self.behaviour == "ping_then_close":
                conn.sendall(frame(3, {"ping": 1}))
                self.received.append(read(conn))          # expect PONG
                conn.sendall(frame(2, {"message": "bye"}))
                return
            while self.running:                            # hold open
                time.sleep(0.05)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self):
        self.running = False
        for c in self.live:
            try:
                c.close()
            except OSError:
                pass
        self.srv.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Base(unittest.TestCase):
    def setUp(self):
        # short path: AF_UNIX paths are limited to ~104 bytes on macOS
        self.tmp = tempfile.mkdtemp(prefix="drpc", dir="/tmp")
        self.p = load(self.tmp)
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.close()

    def fake(self, **kw):
        f = FakeDiscord(self.tmp, **kw)
        self.fakes.append(f)
        return f


class Framing(Base):
    def test_encode_layout(self):
        b = self.p.encode(1, {"a": 1})
        op, n = struct.unpack("<II", b[:8])
        self.assertEqual((op, n), (1, len(b) - 8))
        self.assertEqual(json.loads(b[8:]), {"a": 1})

    def test_config_constants(self):
        self.assertEqual(self.p.CLIENT_ID, "1551699389263904769")
        self.assertTrue(self.p.GIF_URL.startswith("https://raw.githubusercontent.com/gthmhttn/discord-rpc/"))
        self.assertTrue(self.p.GIF_URL.endswith("/typing.gif"))

    def test_activity_shape(self):
        a = self.p.build_activity(1234)
        self.assertEqual(a["assets"]["large_image"], self.p.GIF_URL)
        self.assertEqual(a["timestamps"], {"start": 1234})
        self.assertNotIn("details", a)     # the app name is the only text line


class Timer(Base):
    def test_persist_survives_restart(self):
        first = self.p.start_time_ms(now=1000.0)
        self.assertEqual(first, 1_000_000)
        again = load(self.tmp).start_time_ms(now=5000.0)
        self.assertEqual(again, 1_000_000)

    def test_corrupt_state_starts_fresh(self):
        os.makedirs(self.p.STATE_DIR, exist_ok=True)
        with open(os.path.join(self.p.STATE_DIR, "state.json"), "w") as fh:
            fh.write("{not json")
        self.assertEqual(self.p.start_time_ms(now=42.0), 42_000)

    def test_future_start_ignored(self):
        os.makedirs(self.p.STATE_DIR, exist_ok=True)
        with open(os.path.join(self.p.STATE_DIR, "state.json"), "w") as fh:
            json.dump({"start_ms": 9_999_999_999_999}, fh)
        self.assertEqual(self.p.start_time_ms(now=10.0), 10_000)

    def test_login_mode_never_persists(self):
        p = load(self.tmp, DISCORD_RPC_TIMER="login")
        self.assertEqual(p.start_time_ms(now=7.0), 7000)
        self.assertFalse(os.path.exists(os.path.join(p.STATE_DIR, "state.json")))

    def test_reset_timer_cli(self):
        self.p.start_time_ms(now=1.0)
        self.assertEqual(self.p.main(["--reset-timer"]), 0)
        with open(os.path.join(self.p.STATE_DIR, "state.json")) as fh:
            self.assertGreater(json.load(fh)["start_ms"], 1000)


class Protocol(Base):
    def test_session_sends_handshake_and_activity(self):
        f = self.fake()
        stop = {"flag": False}
        why = self.p.session(stop, deadline=time.time() + 1.5)
        self.assertEqual(why, "deadline")
        (op0, hs), (op1, cmd) = f.received[:2]
        self.assertEqual((op0, hs), (0, {"v": 1, "client_id": "1551699389263904769"}))
        self.assertEqual(op1, 1)
        self.assertEqual(cmd["cmd"], "SET_ACTIVITY")
        self.assertEqual(cmd["args"]["pid"], os.getpid())
        self.assertEqual(cmd["args"]["activity"]["assets"]["large_image"], self.p.GIF_URL)

    def test_rejected_handshake_raises(self):
        self.fake(behaviour="reject_handshake")
        with self.assertRaisesRegex(self.p.ProtocolError, "Invalid Client ID"):
            self.p.session({"flag": False}, deadline=time.time() + 1)

    def test_activity_error_raises(self):
        self.fake(behaviour="error_activity")
        with self.assertRaisesRegex(self.p.ProtocolError, "bad asset"):
            self.p.session({"flag": False}, deadline=time.time() + 1)

    def test_ping_answered_and_close_ends_hold(self):
        f = self.fake(behaviour="ping_then_close")
        why = self.p.session({"flag": False}, deadline=time.time() + 3)
        self.assertEqual(why, "closed by Discord")
        self.assertEqual(f.received[2], (4, {"ping": 1}))   # PONG echoes payload

    def test_no_discord(self):
        with self.assertRaisesRegex(ConnectionError, "not running"):
            self.p.connect()

    def test_stale_socket_file_skipped(self):
        # a leftover socket file with nobody listening, then a live one
        dead = socket.socket(socket.AF_UNIX)
        dead.bind(os.path.join(self.tmp, "discord-ipc-0"))
        dead.close()
        f = FakeDiscord(self.tmp, name="discord-ipc-1")
        self.fakes.append(f)
        sock, path = self.p.connect()
        sock.close()
        self.assertTrue(path.endswith("discord-ipc-1"))


class Loop(Base):
    def test_reconnects_when_discord_restarts(self):
        f1 = self.fake(behaviour="ping_then_close")
        stop = {"flag": False}
        t = threading.Thread(target=self.p.run_forever, args=(stop,), daemon=True)
        t.start()
        deadline = time.time() + 5
        while f1.connections < 2 and time.time() < deadline:
            time.sleep(0.05)
        stop["flag"] = True
        t.join(3)
        self.assertGreaterEqual(f1.connections, 2, "did not reconnect")
        self.assertFalse(t.is_alive(), "did not stop on signal flag")

    def test_waits_quietly_while_discord_is_down(self):
        stop = {"flag": False}
        with self.assertLogs("discord-rpc", level="WARNING") as cm:
            t = threading.Thread(target=self.p.run_forever, args=(stop,), daemon=True)
            t.start()
            time.sleep(1.8)            # ~3 retries
            stop["flag"] = True
            t.join(3)
        self.assertEqual(len(cm.output), 1, "the same problem should be logged once")


class CheckCli(Base):
    def test_check_fails_without_discord(self):
        self.assertEqual(self.p.main(["--check"]), 1)

    def test_check_passes_with_discord(self):
        self.fake()
        orig = self.p.session
        # shorten the 20 s hold for the test
        self.p.session = lambda stop, deadline=None: orig(stop, deadline=time.time() + 0.5)
        try:
            self.assertEqual(self.p.main(["--check"]), 0)
        finally:
            self.p.session = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
