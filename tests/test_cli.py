import unittest
from pathlib import Path
from unittest import mock

from devloop.cli import (
    _build_arg_parser,
    _handle_apply_patch,
    _handle_collect_context,
    _maybe_add_project_tree_summary,
    _resolve_detection,
    _should_include_full_protocol_reference,
)
from devloop.config import DevloopConfig
from devloop.detector import ClipboardKind
from devloop.errors import PatchInfrastructureError
from devloop.protocol import ProtocolCommand
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

    def test_auto_mode_reclassifies_generic_sbt_success_when_session_expects_compile(self) -> None:
        session = SessionState(
            repo_root=str(Path(__file__).resolve().parents[1]),
            session_id="test-session",
            initialized=True,
            last_run_at="2026-01-01T00:00:00+00:00",
            last_parsed_llm_response={
                "next_step_human": "Run compile again.",
                "current_goal_en": "Validate the compile result.",
            },
        )
        text = "\n".join(
            [
                "[info] welcome to sbt 1.12.9",
                "[info] loading settings for project root from build.sbt...",
                "[info] set current project to parboiled2-root",
                "[success] Total time: 2 s, completed Apr 14, 2026, 7:56:58 PM",
            ]
        )
        detection, forced = _resolve_detection(text, "auto", session)
        self.assertFalse(forced)
        self.assertEqual(detection.kind, ClipboardKind.SBT_COMPILE)

    def test_auto_mode_keeps_generic_sbt_success_as_raw_without_compile_context(self) -> None:
        session = SessionState(
            repo_root=str(Path(__file__).resolve().parents[1]),
            session_id="test-session",
            initialized=True,
            last_run_at="2026-01-01T00:00:00+00:00",
            last_parsed_llm_response={
                "next_step_human": "Run tests next.",
                "current_goal_en": "Validate the test result.",
            },
        )
        text = "\n".join(
            [
                "[info] welcome to sbt 1.12.9",
                "[info] loading settings for project root from build.sbt...",
                "[info] set current project to parboiled2-root",
                "[success] Total time: 2 s, completed Apr 14, 2026, 7:56:58 PM",
            ]
        )
        detection, forced = _resolve_detection(text, "auto", session)
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

    def test_collect_context_builds_prompt_without_name_error(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = DevloopConfig(project_root=repo_root, human_language="en")
        command = ProtocolCommand(
            version="1",
            command="COLLECT_CONTEXT",
            summary_human="Collect the required context.",
            next_step_human="Paste the new prompt into ChatGPT.",
            task_summary_en="Inspect the required files.",
            current_goal_en="Read the smallest useful repository context.",
            payload={"queries": [{"type": "read_file", "file": "README.md"}]},
        )
        session = SessionState(
            repo_root=str(repo_root),
            session_id="test-session",
            initialized=True,
            last_run_at="2026-01-01T00:00:00+00:00",
        )
        retriever = mock.Mock()
        retriever.execute_queries.return_value = [QueryResult("read_file", "README", "body")]
        session_store = mock.Mock()

        with mock.patch("devloop.cli.set_clipboard_text") as clipboard_mock:
            with mock.patch("builtins.print") as print_mock:
                _handle_collect_context(command, config, retriever, session_store, session)

        clipboard_mock.assert_called_once()
        session_store.save.assert_called()
        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Collect the required context.", output)
        self.assertIn("A new prompt for ChatGPT was copied to the clipboard.", output)

    def test_local_patch_infrastructure_error_does_not_build_repair_prompt(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        config = DevloopConfig(project_root=repo_root, human_language="en")
        command = ProtocolCommand(
            version="1",
            command="APPLY_PATCH",
            summary_human="Apply the patch.",
            next_step_human="Run compile.",
            task_summary_en="Test patch application.",
            current_goal_en="Apply a minimal patch.",
            payload={"patch_format": "search_replace_v1", "files": []},
        )
        session = SessionState(
            repo_root=str(repo_root),
            session_id="test-session",
            initialized=True,
            last_run_at="2026-01-01T00:00:00+00:00",
        )
        retriever = mock.Mock()
        session_store = mock.Mock()
        session_store.state_dir = repo_root / ".state"

        with mock.patch("devloop.cli.apply_patch_payload", side_effect=PatchInfrastructureError("index.lock denied")):
            with mock.patch("devloop.cli.set_clipboard_text") as clipboard_mock:
                with mock.patch("builtins.print") as print_mock:
                    _handle_apply_patch(command, config, retriever, session_store, session)

        clipboard_mock.assert_not_called()
        output = "\n".join(str(call.args[0]) for call in print_mock.call_args_list if call.args)
        self.assertIn("Patch was not applied because of a local Git or filesystem error.", output)
        self.assertIn("index.lock denied", output)


if __name__ == "__main__":
    unittest.main()
