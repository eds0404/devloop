from pathlib import PurePosixPath
import unittest

from devloop.errors import PatchApplyError
from devloop.patch_apply import validate_repo_relative_path


class PatchApplyTests(unittest.TestCase):
    def test_rejects_unsafe_path(self) -> None:
        with self.assertRaises(PatchApplyError):
            validate_repo_relative_path(PurePosixPath("../outside.scala"))


if __name__ == "__main__":
    unittest.main()
