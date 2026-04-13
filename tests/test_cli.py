import unittest

from devloop.cli import _build_arg_parser, _resolve_detection
from devloop.detector import ClipboardKind


class CliTests(unittest.TestCase):
    def test_accepts_force_flags(self) -> None:
        args = _build_arg_parser().parse_args(
            ["--config", "C:\\repo\\devloop.yaml", "--force-bootstrap", "--force-mode", "compile"]
        )
        self.assertTrue(args.force_bootstrap)
        self.assertEqual(args.force_mode, "compile")

    def test_force_mode_overrides_detection(self) -> None:
        detection, forced = _resolve_detection("plain text", "test")
        self.assertTrue(forced)
        self.assertEqual(detection.kind, ClipboardKind.SBT_TEST)

    def test_auto_mode_keeps_detector(self) -> None:
        detection, forced = _resolve_detection("plain text", "auto")
        self.assertFalse(forced)
        self.assertEqual(detection.kind, ClipboardKind.RAW_TEXT)


if __name__ == "__main__":
    unittest.main()
