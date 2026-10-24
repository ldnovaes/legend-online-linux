import io
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import client


class LauncherTests(unittest.TestCase):
    def test_rejects_unrelated_http_service(self):
        replies = [({"status": 200}, b""), ({"status": 200}, b"<html>other service</html>")]
        with patch.object(client, "probe", side_effect=replies):
            with self.assertRaisesRegex(RuntimeError, "incompatível"):
                client.check_proxy()

    def test_rejects_failed_health_even_with_valid_policy(self):
        replies = [({"status": 503}, b""), ({"status": 200}, b"<cross-domain-policy/>")]
        with patch.object(client, "probe", side_effect=replies):
            with self.assertRaises(RuntimeError):
                client.check_proxy()

    def test_detects_proxy_crash_during_session(self):
        game = Mock()
        game.poll.return_value = None
        proxy = Mock(returncode=7)
        proxy.poll.return_value = 7
        with self.assertRaisesRegex(RuntimeError, "H2Proxy.*7"):
            client.wait_client(game, proxy, 0)

    def test_early_exit_is_not_successful_smoke_test(self):
        game = Mock()
        game.poll.return_value = 0
        with self.assertRaisesRegex(RuntimeError, "antes do fim"):
            client.wait_client(game, None, 15)

    def test_game_error_is_reported(self):
        game = Mock()
        game.poll.return_value = 5
        with self.assertRaisesRegex(RuntimeError, "código 5"):
            client.wait_client(game, None, 0)

    def test_child_output_and_invalid_utf8_reach_log(self):
        with tempfile.TemporaryDirectory() as directory:
            client.configure_logging(Path(directory))
            process, reader = client.launch("test-child", [sys.executable, "-c",
                "import os; os.write(1,b'hello\\n'); os.write(2,b'error \\xff\\n')"],
                directory, os.environ.copy())
            try:
                self.assertEqual(process.wait(timeout=5), 0)
                reader.join(timeout=5)
                self.assertFalse(reader.is_alive())
                text = (Path(directory) / "legend-online.log").read_text()
                self.assertIn("hello", text)
                self.assertIn("error \ufffd", text)
                self.assertIn("test-child pid=", text)
            finally:
                client.stop_owned(process)
                for handler in client.LOG.handlers[:]:
                    client.LOG.removeHandler(handler)
                    handler.close()

    def test_missing_installation_is_logged(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        client.LOG.addHandler(handler)
        client.LOG.setLevel(logging.INFO)
        try:
            with tempfile.TemporaryDirectory() as directory:
                with patch.object(client, "configure_logging"), patch.object(sys, "argv", [
                    "client.py", "run", "--prefix", directory]):
                    self.assertEqual(client.main(), 1)
            self.assertIn("Instalação incompleta", output.getvalue())
            self.assertIn("código=1", output.getvalue())
        finally:
            client.LOG.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    unittest.main()
