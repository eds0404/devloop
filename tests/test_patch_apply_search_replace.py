from pathlib import Path, PurePosixPath
import shutil
import unittest
from unittest import mock
import uuid

from devloop.errors import PatchApplyError, PatchInfrastructureError
from devloop.patch_apply import (
    SearchReplaceFilePlan,
    SearchReplaceOp,
    _apply_exact_replacements,
    _parse_search_replace_payload,
    apply_patch_payload,
)


class PatchApplySearchReplaceTests(unittest.TestCase):
    def _make_repo_root(self) -> Path:
        repo_root = Path(__file__).resolve().parent / "_tmp" / f"repo_{uuid.uuid4().hex}"
        repo_root.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(repo_root, ignore_errors=True))
        return repo_root

    def test_parses_replace_plan(self) -> None:
        payload = {
            "patch_format": "search_replace_v1",
            "files": [
                {
                    "path": "src/main/scala/com/acme/Parser.scala",
                    "replacements": [
                        {
                            "search": "old",
                            "replace": "new",
                            "expected_matches": 1,
                        }
                    ],
                }
            ],
        }
        plans = _parse_search_replace_payload(payload)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].operation, "replace")
        self.assertEqual(plans[0].path.as_posix(), "src/main/scala/com/acme/Parser.scala")

    def test_parses_create_and_delete_operations(self) -> None:
        payload = {
            "patch_format": "search_replace_v1",
            "files": [
                {
                    "path": "src/main/scala/com/acme/NewFile.scala",
                    "operation": "create",
                    "content": "object NewFile {}\n",
                },
                {
                    "path": "src/main/scala/com/acme/OldFile.scala",
                    "operation": "delete",
                    "expected_sha256": "abc123",
                },
            ],
        }
        plans = _parse_search_replace_payload(payload)
        self.assertEqual([plan.operation for plan in plans], ["create", "delete"])
        self.assertEqual(plans[0].content, "object NewFile {}\n")
        self.assertEqual(plans[1].expected_sha256, "abc123")

    def test_apply_patch_payload_rejects_unsupported_patch_format(self) -> None:
        with self.assertRaises(PatchApplyError) as context:
            apply_patch_payload(Path("."), Path("."), {"patch_format": "git_unified_diff"}, allow_apply_on_dirty_files=True)
        self.assertIn("search_replace_v1", str(context.exception))

    def test_exact_replacements_require_expected_matches(self) -> None:
        plan = SearchReplaceFilePlan(
            path=PurePosixPath("src/main/scala/com/acme/Parser.scala"),
            operation="replace",
            expected_sha256=None,
            replacements=[SearchReplaceOp(search="old", replace="new", expected_matches=2)],
            content=None,
        )
        with self.assertRaises(PatchApplyError) as context:
            _apply_exact_replacements("old\r\n", plan)
        self.assertIn("expected 2 match(es)", str(context.exception))

    def test_exact_replacements_preserve_newline_style(self) -> None:
        plan = SearchReplaceFilePlan(
            path=PurePosixPath("src/main/scala/com/acme/Parser.scala"),
            operation="replace",
            expected_sha256=None,
            replacements=[SearchReplaceOp(search="old", replace="new", expected_matches=1)],
            content=None,
        )
        updated, replacement_results = _apply_exact_replacements("old\r\n", plan)
        self.assertEqual(updated, "new\r\n")
        self.assertEqual(replacement_results[0].matched_line_numbers, [1])

    def test_exact_replacements_reject_no_changes(self) -> None:
        plan = SearchReplaceFilePlan(
            path=PurePosixPath("src/main/scala/com/acme/Parser.scala"),
            operation="replace",
            expected_sha256=None,
            replacements=[SearchReplaceOp(search="old", replace="old", expected_matches=1)],
            content=None,
        )
        with self.assertRaises(PatchApplyError) as context:
            _apply_exact_replacements("old\n", plan)
        self.assertIn("produced no changes", str(context.exception))
        self.assertEqual(context.exception.stage, "no_change_check")

    def test_exact_replacements_reports_matched_line_numbers_on_count_mismatch(self) -> None:
        plan = SearchReplaceFilePlan(
            path=PurePosixPath("src/main/scala/com/acme/Parser.scala"),
            operation="replace",
            expected_sha256=None,
            replacements=[SearchReplaceOp(search="old", replace="new", expected_matches=2)],
            content=None,
        )
        with self.assertRaises(PatchApplyError) as context:
            _apply_exact_replacements("x\nold\ny\n", plan)
        self.assertEqual(context.exception.stage, "match_count_check")
        self.assertEqual(context.exception.details["found_matches"], 1)
        self.assertEqual(context.exception.details["matched_line_numbers"], [2])

    def test_refuses_dirty_files_when_not_allowed(self) -> None:
        repo_root = self._make_repo_root()
        target = repo_root / "src" / "main" / "scala" / "com" / "acme" / "Parser.scala"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old\n", encoding="utf-8")
        payload = {
            "patch_format": "search_replace_v1",
            "files": [
                {
                    "path": "src/main/scala/com/acme/Parser.scala",
                    "replacements": [{"search": "old", "replace": "new", "expected_matches": 1}],
                }
            ],
        }
        with mock.patch("devloop.patch_apply.list_dirty_paths", return_value={"src/main/scala/com/acme/Parser.scala"}):
            with self.assertRaises(PatchApplyError) as context:
                apply_patch_payload(repo_root, repo_root, payload, allow_apply_on_dirty_files=False)
        self.assertIn("affected files are dirty", str(context.exception))

    def test_apply_create_and_delete_operations(self) -> None:
        repo_root = self._make_repo_root()
        old_file = repo_root / "src" / "main" / "scala" / "com" / "acme" / "OldFile.scala"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_text("object OldFile {}\n", encoding="utf-8")
        payload = {
            "patch_format": "search_replace_v1",
            "files": [
                {
                    "path": "src/main/scala/com/acme/NewFile.scala",
                    "operation": "create",
                    "content": "object NewFile {}\n",
                },
                {
                    "path": "src/main/scala/com/acme/OldFile.scala",
                    "operation": "delete",
                },
            ],
        }

        def fake_run_git(_repo_root: Path, args: list[str], **_kwargs: object) -> object:
            if args[:2] == ["rm", "--"]:
                (repo_root / args[2]).unlink()
            return mock.Mock(stdout="")

        with mock.patch("devloop.patch_apply.run_git", side_effect=fake_run_git):
            with mock.patch("devloop.patch_apply.summarize_paths_status", return_value="A  src/main/scala/com/acme/NewFile.scala"):
                result = apply_patch_payload(repo_root, repo_root, payload, allow_apply_on_dirty_files=True)

        new_file = repo_root / "src" / "main" / "scala" / "com" / "acme" / "NewFile.scala"
        self.assertTrue(new_file.exists())
        self.assertEqual(new_file.read_text(encoding="utf-8"), "object NewFile {}\n")
        self.assertFalse(old_file.exists())
        self.assertEqual(
            result.affected_files,
            [
                "src/main/scala/com/acme/NewFile.scala",
                "src/main/scala/com/acme/OldFile.scala",
            ],
        )
        self.assertEqual(result.file_results[0].after_sha256 is not None, True)
        self.assertEqual(result.file_results[1].before_sha256 is not None, True)

    def test_replace_only_patch_can_continue_without_staging_on_index_lock(self) -> None:
        repo_root = self._make_repo_root()
        target = repo_root / "src" / "main" / "scala" / "com" / "acme" / "Parser.scala"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("old\n", encoding="utf-8", newline="")
        payload = {
            "patch_format": "search_replace_v1",
            "files": [
                {
                    "path": "src/main/scala/com/acme/Parser.scala",
                    "replacements": [
                        {
                            "search": "old",
                            "replace": "new",
                            "expected_matches": 1,
                        }
                    ],
                }
            ],
        }
        failure_emitted = False

        def fake_run_git(_repo_root: Path, args: list[str], **_kwargs: object) -> object:
            nonlocal failure_emitted
            if args[:2] == ["add", "--"] and not failure_emitted:
                failure_emitted = True
                raise OSError("fatal: Unable to create '.git/index.lock': Permission denied")
            return mock.Mock(stdout="")

        with mock.patch("devloop.patch_apply.run_git", side_effect=fake_run_git):
            result = apply_patch_payload(
                repo_root,
                repo_root,
                payload,
                allow_apply_on_dirty_files=True,
            )

        self.assertIn("Git staging skipped locally", result.warning)
        self.assertIn("git_stage_skipped", result.fallbacks_used)
        self.assertEqual(target.read_text(encoding="utf-8"), "new\n")

    def test_rolls_back_create_file_when_git_add_fails(self) -> None:
        repo_root = self._make_repo_root()
        target = repo_root / "src" / "main" / "scala" / "com" / "acme" / "NewFile.scala"
        payload = {
            "patch_format": "search_replace_v1",
            "files": [
                {
                    "path": "src/main/scala/com/acme/NewFile.scala",
                    "operation": "create",
                    "content": "object NewFile {}\n",
                }
            ],
        }
        failure_emitted = False

        def fake_run_git(_repo_root: Path, args: list[str], **_kwargs: object) -> object:
            nonlocal failure_emitted
            if args[:2] == ["add", "--"] and not failure_emitted:
                failure_emitted = True
                raise OSError("fatal: Unable to create '.git/index.lock': Permission denied")
            return mock.Mock(stdout="")

        with mock.patch("devloop.patch_apply.run_git", side_effect=fake_run_git):
            with self.assertRaises(PatchInfrastructureError) as context:
                apply_patch_payload(
                    repo_root,
                    repo_root,
                    payload,
                    allow_apply_on_dirty_files=True,
                )

        self.assertIn("Permission denied", str(context.exception))
        self.assertTrue(
            "rollback restored the original local file state" in str(context.exception)
            or "rollback failed:" in str(context.exception)
        )
        self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
