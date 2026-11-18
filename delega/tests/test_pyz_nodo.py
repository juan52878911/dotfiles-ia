"""What a node sees: delega run from delega.pyz, and the memory reached through a non-interactive ssh."""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from delega import util  # noqa: E402


class PyzCatalog(unittest.TestCase):
    def test_messages_are_translated_inside_the_pyz(self):
        with tempfile.TemporaryDirectory() as tmp:
            pyz = os.path.join(tmp, "delega.pyz")
            util.build_pyz(pyz)
            env = dict(os.environ, DELEGA_LOCALE="es", PYTHONPATH="")
            code = ("import sys; sys.path.insert(0, %r); from delega import i18n; "
                    "print(i18n.t('mem.init_ok', path='p', org='o'))" % pyz)
            r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("mem.init_ok", r.stdout)
            self.assertIn("org o", r.stdout)


class RemoteMemoryPath(unittest.TestCase):
    def test_ssh_command_puts_local_bin_first(self):
        from delega import queue
        client = queue.MemClient.__new__(queue.MemClient)
        client.owner, client.org = "dev-trycore", "trycore"
        client.m = {"host": "trycorepc", "path": "/x/m.db", "local": False}
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}\n', stderr="")

        with mock.patch("subprocess.run", side_effect=fake_run):
            rc, payload = client._ssh("status")
        self.assertEqual(rc, 0)
        self.assertEqual(payload, {"ok": True})
        self.assertTrue(seen["argv"][-1].startswith('PATH="$HOME/.local/bin:$PATH" '), seen["argv"][-1])


if __name__ == "__main__":
    unittest.main()
