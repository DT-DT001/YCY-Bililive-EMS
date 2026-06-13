import unittest
from unittest.mock import Mock, patch

from backend.desktop import (
    DesktopServer,
    run_browser_compatibility_mode,
    show_compatibility_notice,
)


class DesktopServerTests(unittest.TestCase):
    def test_desktop_server_uses_an_available_dynamic_port(self):
        first = DesktopServer()
        second = DesktopServer()
        try:
            first.start()
            second.start()
            self.assertIsInstance(first.port, int)
            self.assertIsInstance(second.port, int)
            self.assertGreater(first.port, 0)
            self.assertGreater(second.port, 0)
            self.assertNotEqual(first.port, second.port)
        finally:
            second.stop()
            first.stop()

    @patch("backend.desktop.show_compatibility_notice")
    @patch("backend.desktop.webbrowser.open")
    def test_browser_compatibility_mode_opens_server_url(
        self,
        open_browser: Mock,
        show_notice: Mock,
    ):
        server = Mock()
        server.thread = Mock()
        server.thread.is_alive.side_effect = [True, False]
        error = RuntimeError("pythonnet failed")

        run_browser_compatibility_mode(server, "http://127.0.0.1:12345", error)

        open_browser.assert_called_once_with("http://127.0.0.1:12345")
        show_notice.assert_called_once_with(error)
        server.thread.join.assert_called_once_with(timeout=0.5)

    @patch("backend.desktop.ctypes")
    @patch("backend.desktop.os.name", "nt")
    def test_compatibility_notice_contains_original_error(self, ctypes_mock: Mock):
        show_compatibility_notice(RuntimeError("loader failure"))

        message = ctypes_mock.windll.user32.MessageBoxW.call_args.args[1]
        self.assertIn("浏览器兼容模式", message)
        self.assertIn("loader failure", message)


if __name__ == "__main__":
    unittest.main()
