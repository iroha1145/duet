"""Installer boundary tests; root cases run only on a disposable Linux root host."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

SOURCE = Path(__file__).resolve().parents[1]
INSTALLER = SOURCE / "installer/install-server.sh"


@unittest.skipUnless(sys.platform == "linux" and os.geteuid() == 0 and Path("/opt").is_dir(), "Linux root installer preflight")
class RootPreflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="duet-preflight-", dir="/opt")
        self.base = Path(self.temp.name)
        self.base.chmod(0o755)
        self.root = self.base / "install"
        self.user = "duet-test-" + uuid.uuid4().hex[:12]
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.args = ["bash", str(INSTALLER), "--root", str(self.root), "--service-user", self.user,
                     "--public-url", "https://duet.example.com", "--username", "owner", "--port", str(self.port), "--check"]

    def tearDown(self):
        self.temp.cleanup()

    def check(self, *extra, env=None):
        return subprocess.run(self.args + list(extra), capture_output=True, text=True, env=env)

    def test_first_check_creates_no_user_directory_or_unit(self):
        result = self.check("--no-systemd")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.root.exists())
        self.assertNotEqual(subprocess.run(["getent", "passwd", self.user], capture_output=True).returncode, 0)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_foreign_nonempty_path_and_symlink_rejected(self):
        self.root.mkdir()
        (self.root / "foreign.txt").write_text("retain")
        result = self.check("--no-systemd")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.root / "foreign.txt").read_text(), "retain")
        alias = self.base / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.args[self.args.index("--root") + 1] = str(alias)
        self.assertNotEqual(self.check("--no-systemd").returncode, 0)

    def test_foreign_system_unit_is_not_adopted(self):
        # A mock reports an existing vendor unit; no /etc writes or systemd changes.
        binary = self.base / "bin"
        binary.mkdir()
        mock = binary / "systemctl"
        mock.write_text("#!/bin/sh\nexit 0\n")
        mock.chmod(0o755)
        result = self.check(env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"]})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already exists outside", result.stderr)
        self.assertFalse(self.root.exists())

    def test_existing_external_account_rejected(self):
        self.args[self.args.index("--service-user") + 1] = "nobody"
        self.assertNotEqual(self.check("--no-systemd").returncode, 0)
        self.assertFalse(self.root.exists())

    def test_occupied_port_rejected_before_changes(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", self.port))
            sock.listen()
            result = self.check("--no-systemd")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("already in use", result.stderr)
        self.assertFalse(self.root.exists())


@unittest.skipUnless(sys.platform == "linux", "SQLite Unix byte-lock snapshots")
class AccountPreflightSnapshot(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "auth.sqlite3"
        script = INSTALLER.read_text()
        self.probe = script[script.index("import contextlib\nimport errno\nimport fcntl"):].split("\nPY\n", 1)[0]

    def tearDown(self):
        self.temp.cleanup()

    def inspect(self):
        return subprocess.run([sys.executable, "-", str(self.path)], input=self.probe, text=True, capture_output=True, timeout=10)

    def files(self):
        return {item.name: (item.read_bytes(), item.stat().st_uid, item.stat().st_gid) for item in self.path.parent.iterdir()}

    def writer(self, busy=False):
        # A separate process is essential: POSIX record locks are per process.
        code = """import sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('PRAGMA wal_autocheckpoint=0')
connection.execute('CREATE TABLE account(singleton INTEGER PRIMARY KEY)')
connection.commit()
connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
connection.execute('INSERT INTO account VALUES(1)')
connection.commit()
if sys.argv[2] == 'busy':
    connection.execute('BEGIN IMMEDIATE')
print('ready', flush=True)
sys.stdin.readline()
connection.close()
"""
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path), "busy" if busy else "live"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.finish, child)
        self.assertEqual(child.stdout.readline().strip(), "ready")
        return child

    def finish(self, child):
        if child.poll() is None:
            child.communicate("stop\n", timeout=5)

    def test_clean_stop_check_does_not_create_sidecars(self):
        child = self.writer()
        self.finish(child)
        self.assertEqual(set(self.files()), {"auth.sqlite3"})
        before = self.files()
        result = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertEqual(self.files(), before)

    def test_live_account_only_in_wal_is_retained_without_changes(self):
        import sqlite3
        self.writer()
        # Demonstrate why immutable=1 would be incorrect for this fixture.
        with sqlite3.connect(self.path.as_uri() + '?immutable=1', uri=True) as main_only:
            self.assertIsNone(main_only.execute('SELECT 1 FROM account').fetchone())
        before = self.files()
        result = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")
        self.assertEqual(self.files(), before)

    def test_write_lock_contention_fails_without_changes(self):
        self.writer(busy=True)
        before = self.files()
        result = self.inspect()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("database is busy", result.stderr)
        self.assertEqual(self.files(), before)

    def test_corrupt_database_fails_without_changes(self):
        self.path.write_bytes(b'fixture invalid sqlite database')
        before = self.files()
        result = self.inspect()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.files(), before)


class EmbeddedInstallerPython(unittest.TestCase):
    def test_embedded_python_compiles(self):
        blocks = INSTALLER.read_text().split("<<'PY'\n")[1:]
        self.assertGreaterEqual(len(blocks), 4)
        for block in blocks:
            compile(block.split("\nPY\n", 1)[0], str(INSTALLER), "exec")

    def test_root_foreground_command_drops_privileges(self):
        # Execute the actual output section with harmless fixture paths.
        bash = shutil.which("bash") or ("C:/Program Files/Git/bin/bash.exe" if os.name == "nt" else None)
        if not bash or not Path(bash).exists():
            self.skipTest("bash unavailable")
        source = INSTALLER.read_text()
        section = source[source.index("if ((no_systemd)); then\n  prefix=()"):]
        script = "set -euo pipefail\nno_systemd=1; scope=system; service_user=fixture-user; release=/opt/duet/releases/fixture; port=48766; install_root=/opt/duet; public_url=https://duet.example.com\n" + section
        result = subprocess.run([bash, "-c", script], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("runuser -u fixture-user -- setpriv --no-new-privs /opt/duet/releases/fixture/venv/bin/python", result.stdout)
        self.assertNotIn("systemctl --user", result.stdout)


if __name__ == "__main__":
    unittest.main()
