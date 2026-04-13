import subprocess
from unittest import mock
import unittest

from devloop.clipboard import get_clipboard_text, set_clipboard_text
from devloop.errors import ClipboardError


class ClipboardTests(unittest.TestCase):
    @mock.patch("devloop.clipboard._resolve_powershell_executable", return_value="powershell")
    @mock.patch("devloop.clipboard._ensure_windows")
    @mock.patch("devloop.clipboard.subprocess.run")
    def test_get_clipboard_uses_powershell(self, run_mock: mock.Mock, _: mock.Mock, __: mock.Mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=["powershell"],
            returncode=0,
            stdout="hello".encode("utf-8"),
            stderr=b"",
        )

        text = get_clipboard_text()

        self.assertEqual(text, "hello")
        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], "powershell")
        self.assertIn("Get-Clipboard -Raw", command[-1])

    @mock.patch("devloop.clipboard._resolve_powershell_executable", return_value="powershell")
    @mock.patch("devloop.clipboard._ensure_windows")
    @mock.patch("devloop.clipboard.subprocess.run")
    def test_set_clipboard_uses_powershell(self, run_mock: mock.Mock, _: mock.Mock, __: mock.Mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess(
            args=["powershell"],
            returncode=0,
            stdout=b"",
            stderr=b"",
        )

        set_clipboard_text("Привет")

        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], "powershell")
        self.assertIn("Set-Clipboard -Value $text", command[-1])
        self.assertEqual(run_mock.call_args.kwargs["input"], "Привет".encode("utf-8"))

    @mock.patch("devloop.clipboard._resolve_powershell_executable", return_value="powershell")
    @mock.patch("devloop.clipboard._ensure_windows")
    @mock.patch("devloop.clipboard.subprocess.run")
    def test_wraps_powershell_errors(self, run_mock: mock.Mock, _: mock.Mock, __: mock.Mock) -> None:
        run_mock.side_effect = subprocess.CalledProcessError(
            returncode=1,
            cmd=["powershell"],
            stderr="boom".encode("utf-8"),
        )

        with self.assertRaises(ClipboardError) as context:
            get_clipboard_text()

        self.assertIn("boom", str(context.exception))


if __name__ == "__main__":
    unittest.main()
