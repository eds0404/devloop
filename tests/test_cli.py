import unittest
from pathlib import Path
from unittest import mock

from devloop.cli import (
    _build_arg_parser,
    _maybe_add_project_tree_summary,
    _resolve_detection,
    _should_include_full_protocol_reference,
)
from devloop.config import DevloopConfig
from devloop.detector import ClipboardKind
from devloop.retrieval import QueryResult
from devloop.session import SessionState


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

    def test_does_not_add_project_summary_by_default(self) -> None:
        config = DevloopConfig(project_root=Path(__file__).resolve().parents[1])
        retriever = mock.Mock()
        query_results = [QueryResult("raw_clipboard", "Raw clipboard content", "body")]
        _maybe_add_project_tree_summary(query_results, retriever, config)
        self.assertEqual(len(query_results), 1)
        retriever.project_tree_summary.assert_not_called()

    def test_adds_project_summary_when_enabled(self) -> None:
        config = DevloopConfig(
            project_root=Path(__file__).resolve().parents[1],
            include_project_summary_in_prompts=True,
        )
        retriever = mock.Mock()
        retriever.project_tree_summary.return_value = "- src/main/scala/App.scala"
        query_results = [QueryResult("raw_clipboard", "Raw clipboard content", "body")]
        _maybe_add_project_tree_summary(query_results, retriever, config)
        self.assertEqual(len(query_results), 2)
        self.assertEqual(query_results[-1].query_type, "project_tree")
        retriever.project_tree_summary.assert_called_once()

    def test_full_protocol_reference_is_included_every_other_followup_prompt(self) -> None:
        session = SessionState(
            repo_root=str(Path(__file__).resolve().parents[1]),
            session_id="test-session",
            initialized=True,
            last_run_at="2026-01-01T00:00:00+00:00",
        )
        self.assertTrue(_should_include_full_protocol_reference(session))
        session.note_followup_prompt_generated()
        self.assertFalse(_should_include_full_protocol_reference(session))
        session.note_followup_prompt_generated()
        self.assertTrue(_should_include_full_protocol_reference(session))


if __name__ == "__main__":
    unittest.main()
