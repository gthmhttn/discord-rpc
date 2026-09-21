"""Dry-run install.sh on any OS: launchctl / plutil / xcode-select are stubs."""
import os
import plistlib
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = "com.chun.discord-rpc"


class Installer(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="drpch")
        self.bin = os.path.join(self.home, "stubbin")
        os.makedirs(self.bin)
        self.calls = os.path.join(self.home, "calls.log")
        self.log = os.path.join(self.home, "Library/Logs/discord-rpc.log")
        self.plist = os.path.join(self.home, "Library/LaunchAgents", LABEL + ".plist")
        # launchctl stub: records calls; on bootstrap simulates the agent
        # writing its log line (unless STUB_AGENT_SILENT=1)
        self.stub("launchctl", f"""
            echo "launchctl $*" >> "{self.calls}"
            if [ "$1" = bootstrap ] && [ "${{STUB_AGENT_SILENT:-0}}" != 1 ]; then
              mkdir -p "$(dirname "{self.log}")"
              echo "x INFO presence set via /tmp/discord-ipc-0 (user chun)" >> "{self.log}"
            fi
            if [ "$1" = print ]; then exit ${{STUB_LOADED_RC:-1}}; fi
            exit 0""")
        self.stub("plutil", f"""
            {sys.executable} -c 'import plistlib,sys; plistlib.load(open(sys.argv[2],"rb"))' "$@" """)
        self.stub("xcode-select", "exit 1")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as fh:
            fh.write("#!/bin/bash\n" + textwrap.dedent(body) + "\n")
        os.chmod(path, 0o755)

    def run_install(self, *args, **env):
        e = dict(os.environ, HOME=self.home, PATH=self.bin + ":" + os.environ["PATH"],
                 PYTHON=sys.executable, DISCORD_RPC_SKIP_CHECK="1")
        e.update(env)
        return subprocess.run(["bash", os.path.join(ROOT, "install.sh"), *args],
                              env=e, capture_output=True, text=True, timeout=60)

    def calls_text(self):
        if not os.path.exists(self.calls):
            return ""
        with open(self.calls) as fh:
            return fh.read()

    def test_install_writes_valid_plist_and_verifies(self):
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verified", r.stdout)
        with open(self.plist, "rb") as fh:
            pl = plistlib.load(fh)
        dest = os.path.join(self.home, "Library/Application Support/discord-rpc/presence.py")
        self.assertEqual(pl["Label"], LABEL)
        self.assertEqual(pl["ProgramArguments"], [sys.executable, dest])
        self.assertTrue(pl["RunAtLoad"] and pl["KeepAlive"])
        self.assertTrue(os.path.isfile(dest))
        c = self.calls_text()
        self.assertIn(f"bootout gui/{os.getuid()}/{LABEL}", c)
        self.assertIn(f"bootstrap gui/{os.getuid()} {self.plist}", c)
        self.assertLess(c.index("bootout"), c.index("bootstrap"))

    def test_old_log_line_does_not_count_as_verified(self):
        os.makedirs(os.path.dirname(self.log))
        with open(self.log, "w") as fh:
            fh.write("old INFO presence set via /tmp/discord-ipc-0 (user chun)\n")
        r = self.run_install(STUB_AGENT_SILENT="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("WARNING", r.stdout)

    def test_failed_check_starts_nothing(self):
        # real --check with no Discord socket reachable
        r = self.run_install(DISCORD_RPC_SKIP_CHECK="0",
                             DISCORD_IPC_DIR=tempfile.mkdtemp(prefix="drpcnone", dir="/tmp"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("check failed", r.stderr)
        self.assertFalse(os.path.exists(self.plist))
        self.assertNotIn("bootstrap", self.calls_text())

    def test_reinstall_is_idempotent(self):
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.run_install().returncode, 0)
        self.assertEqual(self.calls_text().count("bootstrap"), 2)

    def test_uninstall_keeps_timer(self):
        self.run_install()
        state = os.path.join(self.home, "Library/Application Support/discord-rpc/state.json")
        with open(state, "w") as fh:
            fh.write('{"start_ms": 1}')
        r = self.run_install("--uninstall")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.plist))
        self.assertTrue(os.path.exists(state))

    def test_status(self):
        self.run_install()
        r = self.run_install("--status", STUB_LOADED_RC="0")
        self.assertIn("agent: loaded", r.stdout)
        self.assertIn("presence set", r.stdout)

    def test_unknown_option(self):
        r = self.run_install("--bogus")
        self.assertNotEqual(r.returncode, 0)

    def test_no_python_found(self):
        r = self.run_install(PYTHON="")
        # on this Linux box /opt/homebrew and /usr/local python are absent and
        # xcode-select fails, so it must refuse rather than guess
        if not (os.path.exists("/opt/homebrew/bin/python3") or os.path.exists("/usr/local/bin/python3")):
            self.assertEqual(r.returncode, 1)
            self.assertIn("no python3 found", r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
