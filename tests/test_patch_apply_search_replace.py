from pathlib import PurePosixPath
import unittest

from devloop.errors import PatchApplyError
from devloop.patch_apply import (
    SearchReplaceFilePlan,
    SearchReplaceOp,
    _apply_exact_replacements,
    _parse_search_replace_payload,
)


class PatchApplySearchReplaceTests(unittest.TestCase):
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
        updated = _apply_exact_replacements("old\r\n", plan)
        self.assertEqual(updated, "new\r\n")


if __name__ == "__main__":
    unittest.main()
