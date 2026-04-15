from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from devloop.runlog import RunLogRecorder, default_log_path_for_config


class RunLogTests(unittest.TestCase):
    def test_default_log_path_is_hidden_file_next_to_config(self) -> None:
        config_path = Path(r"C:\repo\project\devloop.yaml")
        self.assertEqual(default_log_path_for_config(config_path), Path(r"C:\repo\project\.devloop.log"))

    def test_finalize_appends_flat_entry(self) -> None:
        with TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "devloop.yaml"
            config_path.write_text("project_root: C:\\repo\n", encoding="utf-8")
            recorder = RunLogRecorder(
                config_path=config_path,
                argv=["--config", str(config_path), "--force-bootstrap"],
                started_at=datetime(2026, 4, 15, 10, 30, tzinfo=timezone.utc),
                devloop_head="abc123",
            )
            recorder.record_clipboard_before("before")
            recorder.record_clipboard_after("after")
            recorder.append_console("line one\nline two\n")
            recorder.add_section("PATCH VALIDATION", "Patch decision: accepted")

            recorder.finalize(0)
            recorder.finalize(1)

            log_text = (Path(temp_dir) / ".devloop.log").read_text(encoding="utf-8")
            self.assertEqual(log_text.count("========== DEVLOOP RUN =========="), 2)
            self.assertIn("Devloop HEAD: abc123", log_text)
            self.assertIn("Arguments:\n  [0] --config", log_text)
            self.assertIn("----- BEGIN CLIPBOARD BEFORE -----\nbefore\n----- END CLIPBOARD BEFORE -----", log_text)
            self.assertIn("----- BEGIN CONFIG FILE -----\nproject_root: C:\\repo\n", log_text)
            self.assertIn("----- BEGIN CONSOLE OUTPUT -----\nline one\nline two\n", log_text)
            self.assertIn("----- BEGIN CLIPBOARD AFTER -----\nafter\n----- END CLIPBOARD AFTER -----", log_text)
            self.assertIn("----- BEGIN PATCH VALIDATION -----\nPatch decision: accepted\n----- END PATCH VALIDATION -----", log_text)
            self.assertIn("Exit code: 0", log_text)
            self.assertIn("Exit code: 1", log_text)


if __name__ == "__main__":
    unittest.main()
