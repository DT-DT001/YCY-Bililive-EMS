import unittest

from backend.desktop import DesktopServer


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


if __name__ == "__main__":
    unittest.main()
