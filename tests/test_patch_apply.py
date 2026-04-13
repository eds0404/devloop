from pathlib import PurePosixPath
import unittest

from devloop.errors import PatchApplyError
from devloop.patch_apply import extract_patch_targets, validate_repo_relative_path


PATCH_TEXT = """
diff --git a/src/main/scala/com/acme/Parser.scala b/src/main/scala/com/acme/Parser.scala
index 1111111..2222222 100644
--- a/src/main/scala/com/acme/Parser.scala
+++ b/src/main/scala/com/acme/Parser.scala
@@ -1,3 +1,3 @@
-old
+new
""".strip()


class PatchApplyTests(unittest.TestCase):
    def test_extracts_affected_files(self) -> None:
        targets = extract_patch_targets(PATCH_TEXT)
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].path.as_posix(), "src/main/scala/com/acme/Parser.scala")

    def test_rejects_unsafe_path(self) -> None:
        with self.assertRaises(PatchApplyError):
            validate_repo_relative_path(PurePosixPath("../outside.scala"))


if __name__ == "__main__":
    unittest.main()

